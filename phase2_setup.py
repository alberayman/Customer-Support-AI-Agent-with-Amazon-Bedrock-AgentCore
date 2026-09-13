#!/usr/bin/env python3
"""
phase2_setup.py — build everything except the Knowledge Base.

Creates, in dependency order:
  1. Lambda execution role
  2. order-tracker + refund-processor Lambdas
  3. REST API with 3 GET operations  (WITH method responses — see E7)
  4. Gateway execution role
  5. AgentCore Gateway (NONE authorizer)
  6. Two Gateway targets (API Gateway + Lambda)
  7. AgentCore Memory resource with two strategies

Writes phase2_outputs.json — the source of the config values for main.py.
The Knowledge Base is deliberately NOT created here: it is the expensive
resource, so it goes up last and comes down first.

Every step is idempotent — safe to re-run after a partial failure.

Usage:
  uv run python phase2_setup.py                 # build everything
  uv run python phase2_setup.py --inspect       # print the AgentCore API
                                                #   shapes (run this first if
                                                #   a create_* call is rejected)
  uv run python phase2_setup.py --fix-runtime-role <role-name>
                                                # attach the runtime policy (E8)
                                                #   AFTER agentcore deploy
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Configuration ─────────────────────────────────────────────────────────────
REGION = os.environ.get("AWS_REGION", "us-east-1")

LAMBDA_ROLE_NAME = "csai-lambda-exec-role"
GATEWAY_ROLE_NAME = "csai-gateway-exec-role"
ORDER_FN = "order-tracker"
REFUND_FN = "refund-processor"
REST_API_NAME = "csai-support-api"
STAGE_NAME = "prod"
GATEWAY_NAME = "CustomerSupportGateway"
MEMORY_NAME = "CustomerSupportMemory"

HERE = Path(__file__).parent
LAMBDA_DIR = HERE / "lambda"
OUTPUTS = HERE / "phase2_outputs.json"

# The three GET routes and the operation names the Gateway turns into MCP tools.
ROUTES = [
    ("orders/{order_id}", "get_order",
     "Look up a single order by its order ID. Returns status, items, total, "
     "tracking number, carrier and estimated delivery."),
    ("customers/{customer_id}/orders", "get_customer_orders",
     "List every order belonging to a customer ID."),
    ("customers/{customer_id}", "get_customer",
     "Get a customer profile: name, email, loyalty tier and points balance."),
]

iam = boto3.client("iam", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
apigw = boto3.client("apigateway", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)


# ── Small helpers ─────────────────────────────────────────────────────────────
def log(step, msg):
    print(f"[{step}] {msg}", flush=True)


def load_state():
    if OUTPUTS.exists():
        return json.loads(OUTPUTS.read_text())
    return {}


def save_state(state):
    OUTPUTS.write_text(json.dumps(state, indent=2))
    log("state", f"wrote {OUTPUTS.name}")


def ensure_role(name, service_principal, policy_doc, description):
    """Create an IAM role if absent; always (re)put the inline policy."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": service_principal},
            "Action": "sts:AssumeRole",
        }],
    }
    try:
        resp = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=description,
        )
        arn = resp["Role"]["Arn"]
        log("iam", f"created role {name}")
        time.sleep(10)  # IAM propagation
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        log("iam", f"role {name} already exists")

    iam.put_role_policy(
        RoleName=name,
        PolicyName=f"{name}-inline",
        PolicyDocument=json.dumps(policy_doc),
    )
    log("iam", f"inline policy applied to {name}")
    return arn


def zip_source(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("lambda_function.py", path.read_text())
    return buf.getvalue()


# ── Step 1 — Lambda execution role ────────────────────────────────────────────
def step1_lambda_role():
    return ensure_role(
        LAMBDA_ROLE_NAME,
        "lambda.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                           "logs:PutLogEvents"],
                "Resource": "arn:aws:logs:*:*:*",
            }],
        },
        "Execution role for the customer support Lambdas",
    )


# ── Step 2 — Lambdas ──────────────────────────────────────────────────────────
def step2_lambdas(role_arn):
    arns = {}
    for fn_name, src in ((ORDER_FN, "order_tracker.py"),
                         (REFUND_FN, "refund_processor.py")):
        src_path = LAMBDA_DIR / src
        if not src_path.exists():
            sys.exit(f"ERROR: {src_path} not found — run this from the project folder.")
        code = zip_source(src_path)
        try:
            resp = lam.create_function(
                FunctionName=fn_name,
                Runtime="python3.12",
                Role=role_arn,
                Handler="lambda_function.lambda_handler",
                Code={"ZipFile": code},
                Timeout=30,
                MemorySize=256,
            )
            arns[fn_name] = resp["FunctionArn"]
            log("lambda", f"created {fn_name}")
        except lam.exceptions.ResourceConflictException:
            lam.update_function_code(FunctionName=fn_name, ZipFile=code)
            arns[fn_name] = lam.get_function(
                FunctionName=fn_name)["Configuration"]["FunctionArn"]
            log("lambda", f"{fn_name} exists — code updated")
        lam.get_waiter("function_active_v2").wait(FunctionName=fn_name)
    return arns


# ── Step 3 — REST API ─────────────────────────────────────────────────────────
def _get_or_create_resource(api_id, parent_id, part):
    kids = apigw.get_resources(restApiId=api_id, limit=500)["items"]
    for r in kids:
        if r.get("parentId") == parent_id and r.get("pathPart") == part:
            return r["id"]
    return apigw.create_resource(
        restApiId=api_id, parentId=parent_id, pathPart=part)["id"]


def step3_rest_api(order_fn_arn):
    account = sts.get_caller_identity()["Account"]

    existing = [a for a in apigw.get_rest_apis(limit=500)["items"]
                if a["name"] == REST_API_NAME]
    if existing:
        api_id = existing[0]["id"]
        log("apigw", f"REST API {REST_API_NAME} exists ({api_id})")
    else:
        api_id = apigw.create_rest_api(
            name=REST_API_NAME,
            description="Customer support backend for the AgentCore Gateway",
            endpointConfiguration={"types": ["REGIONAL"]},
        )["id"]
        log("apigw", f"created REST API {api_id}")

    root = [r for r in apigw.get_resources(restApiId=api_id, limit=500)["items"]
            if r["path"] == "/"][0]["id"]

    uri = (f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
           f"{order_fn_arn}/invocations")

    for path, op_name, _desc in ROUTES:
        parent = root
        for part in path.split("/"):
            parent = _get_or_create_resource(api_id, parent, part)
        res_id = parent

        # --- method
        try:
            apigw.put_method(
                restApiId=api_id, resourceId=res_id, httpMethod="GET",
                authorizationType="NONE",
                operationName=op_name,          # becomes the MCP tool name
                requestParameters={},
            )
        except apigw.exceptions.ConflictException:
            apigw.update_method(
                restApiId=api_id, resourceId=res_id, httpMethod="GET",
                patchOperations=[{"op": "replace", "path": "/operationName",
                                  "value": op_name}],
            )

        # --- integration
        apigw.put_integration(
            restApiId=api_id, resourceId=res_id, httpMethod="GET",
            type="AWS_PROXY", integrationHttpMethod="POST", uri=uri,
        )

        # --- METHOD RESPONSES.  This is E7: without them the exported OpenAPI
        #     has no `responses` block and the Gateway REJECTS the target.
        for code in ("200", "404", "500"):
            try:
                apigw.put_method_response(
                    restApiId=api_id, resourceId=res_id, httpMethod="GET",
                    statusCode=code,
                    responseModels={"application/json": "Empty"},
                )
            except apigw.exceptions.ConflictException:
                pass
        log("apigw", f"configured GET /{path} -> {op_name} (200/404/500)")

    # --- allow API Gateway to invoke the Lambda
    try:
        lam.add_permission(
            FunctionName=ORDER_FN,
            StatementId="apigw-invoke",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{REGION}:{account}:{api_id}/*/GET/*",
        )
        log("lambda", "granted API Gateway invoke permission")
    except lam.exceptions.ResourceConflictException:
        pass

    apigw.create_deployment(restApiId=api_id, stageName=STAGE_NAME)
    log("apigw", f"deployed to stage '{STAGE_NAME}'")

    return {
        "rest_api_id": api_id,
        "stage": STAGE_NAME,
        "invoke_url": f"https://{api_id}.execute-api.{REGION}.amazonaws.com/{STAGE_NAME}",
    }


# ── Step 4 — Gateway role ─────────────────────────────────────────────────────
def step4_gateway_role(refund_fn_arn):
    return ensure_role(
        GATEWAY_ROLE_NAME,
        "bedrock-agentcore.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "execute-api:Invoke",
                 "Resource": "*"},
                {"Effect": "Allow", "Action": "lambda:InvokeFunction",
                 "Resource": refund_fn_arn},
            ],
        },
        "Execution role for the AgentCore Gateway",
    )


# ── Steps 5 & 6 — Gateway and targets ─────────────────────────────────────────
def _control_client():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def step5_gateway(role_arn):
    cp = _control_client()
    for g in cp.list_gateways(maxResults=100).get("items", []):
        if g.get("name") == GATEWAY_NAME:
            gid = g["gatewayId"]
            log("gateway", f"{GATEWAY_NAME} exists ({gid})")
            full = cp.get_gateway(gatewayIdentifier=gid)
            return gid, full.get("gatewayUrl")

    resp = cp.create_gateway(
        name=GATEWAY_NAME,
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="NONE",
        description="MCP front door for the customer support backend",
    )
    gid = resp["gatewayId"]
    log("gateway", f"created {GATEWAY_NAME} ({gid})")
    time.sleep(5)
    return gid, resp.get("gatewayUrl")


def step6_targets(gateway_id, api_info, refund_fn_arn, role_arn):
    cp = _control_client()
    existing = {t["name"]: t for t in
                cp.list_gateway_targets(gatewayIdentifier=gateway_id,
                                        maxResults=100).get("items", [])}

    # --- Target A: the REST API
    # Shape verified against botocore 1.43.93:
    #   apiGateway = { restApiId, stage, apiGatewayToolConfiguration }
    #   toolFilters  item = { filterPath, methods[] }      (required)
    #   toolOverrides item = { name, description, path, method }
    if "order-tracker" not in existing:
        cp.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name="order-tracker",
            targetConfiguration={
                "mcp": {
                    "apiGateway": {
                        "restApiId": api_info["rest_api_id"],
                        "stage": api_info["stage"],
                        "apiGatewayToolConfiguration": {
                            "toolFilters": [
                                {"filterPath": f"/{p}", "methods": ["GET"]}
                                for p, _, _ in ROUTES
                            ],
                            "toolOverrides": [
                                {"name": op, "description": d,
                                 "path": f"/{p}", "method": "GET"}
                                for p, op, d in ROUTES
                            ],
                        },
                    }
                }
            },
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        log("gateway", "created target order-tracker")
    else:
        log("gateway", "target order-tracker exists")

    # --- Target B: the refund Lambda
    if "refund-processor" not in existing:
        schema = json.loads((LAMBDA_DIR / "lambda_schema").read_text())
        cp.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name="refund-processor",
            targetConfiguration={
                "mcp": {
                    "lambda": {
                        "lambdaArn": refund_fn_arn,
                        "toolSchema": {"inlinePayload": schema},
                    }
                }
            },
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        log("gateway", "created target refund-processor")
    else:
        log("gateway", "target refund-processor exists")

    # --- report status: a FAILED target is invisible from the agent side (E6)
    time.sleep(10)
    for t in cp.list_gateway_targets(gatewayIdentifier=gateway_id,
                                     maxResults=100).get("items", []):
        status = t.get("status")
        marker = "OK " if status == "READY" else ">>>"
        log("gateway", f"{marker} target {t['name']}: {status}")
        if status == "FAILED":
            detail = cp.get_gateway_target(gatewayIdentifier=gateway_id,
                                           targetId=t["targetId"])
            print(json.dumps(detail.get("statusReasons", []), indent=2))


# ── Step 7 — Memory ───────────────────────────────────────────────────────────
def step7_memory():
    from bedrock_agentcore.memory import MemoryClient
    mc = MemoryClient(region_name=REGION)

    for m in mc.list_memories():
        if m.get("id", "").startswith(MEMORY_NAME):
            log("memory", f"{m['id']} already exists")
            return m["id"]

    log("memory", "creating memory resource (this takes a couple of minutes)")
    mem = mc.create_memory_and_wait(
        name=MEMORY_NAME,
        description="Long-term customer support memory",
        strategies=[
            {"semanticMemoryStrategy": {
                "name": "customer_facts",
                "namespaces": ["cs_agent/{actorId}/facts"]}},
            {"userPreferenceMemoryStrategy": {
                "name": "customer_preferences",
                "namespaces": ["cs_agent/{actorId}/preferences"]}},
        ],
    )
    mid = mem["id"]
    log("memory", f"created {mid}")
    return mid


# ── E8 — runtime execution role policy ────────────────────────────────────────
RUNTIME_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Sid": "Memory", "Effect": "Allow", "Action": [
            "bedrock-agentcore:GetMemory",
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:ListMemoryRecords",
            "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:ListMemoryStrategies",
            "bedrock-agentcore:GetMemoryRecord",
        ], "Resource": "*"},
        {"Sid": "Gateway", "Effect": "Allow", "Action": [
            "bedrock-agentcore:InvokeGateway",
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:ListGatewayTargets",
        ], "Resource": "*"},
        {"Sid": "CodeInterpreterAndBrowser", "Effect": "Allow", "Action": [
            "bedrock-agentcore:CreateCodeInterpreter",
            "bedrock-agentcore:StartCodeInterpreterSession",
            "bedrock-agentcore:InvokeCodeInterpreter",
            "bedrock-agentcore:StopCodeInterpreterSession",
            "bedrock-agentcore:GetCodeInterpreterSession",
            "bedrock-agentcore:CreateBrowser",
            "bedrock-agentcore:StartBrowserSession",
            "bedrock-agentcore:StopBrowserSession",
            "bedrock-agentcore:GetBrowserSession",
            "bedrock-agentcore:ConnectBrowserAutomationStream",
        ], "Resource": "*"},
        {"Sid": "Bedrock", "Effect": "Allow", "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Retrieve",
            "bedrock:RetrieveAndGenerate",
        ], "Resource": "*"},
        {"Sid": "Logs", "Effect": "Allow", "Action": [
            "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
        ], "Resource": "*"},
    ],
}


def fix_runtime_role(role_name):
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="csai-agentcore-runtime-policy",
        PolicyDocument=json.dumps(RUNTIME_POLICY),
    )
    print(f"Attached csai-agentcore-runtime-policy to {role_name}.")
    print("IAM is read live — wait ~60s, then invoke. No redeploy needed.")


# ── --inspect: print the real API shapes ──────────────────────────────────────
def inspect_api():
    """Print the actual input shapes for the AgentCore control-plane calls.

    Run this if create_gateway / create_gateway_target is rejected — the
    parameter names move between botocore releases.
    """
    cp = _control_client()
    model = cp.meta.service_model
    for op in ("CreateGateway", "CreateGatewayTarget", "CreateMemory"):
        try:
            shape = model.operation_model(op).input_shape
        except Exception as e:
            print(f"\n=== {op}: not available ({e}) ===")
            continue
        print(f"\n=== {op} ===")
        print("required:", shape.metadata.get("required", []))
        for member, ms in shape.members.items():
            print(f"  {member}: {ms.type_name}"
                  + (f" {list(ms.members)}" if ms.type_name == "structure" else "")
                  + (f" enum={ms.metadata.get('enum')}"
                     if ms.metadata.get("enum") else ""))
    print(f"\nbotocore has bedrock-agentcore-control: OK (region {REGION})")


# ── main ──────────────────────────────────────────────────────────────────────
def build():
    state = load_state()
    print(f"Account {sts.get_caller_identity()['Account']} / region {REGION}\n")

    state["lambda_role_arn"] = step1_lambda_role()
    state.update(step2_lambdas(state["lambda_role_arn"]))
    save_state(state)

    state["api"] = step3_rest_api(state[ORDER_FN])
    save_state(state)

    state["gateway_role_arn"] = step4_gateway_role(state[REFUND_FN])
    gid, gurl = step5_gateway(state["gateway_role_arn"])
    state["gateway_id"], state["gateway_url"] = gid, gurl
    save_state(state)

    step6_targets(gid, state["api"], state[REFUND_FN], state["gateway_role_arn"])
    state["memory_id"] = step7_memory()
    save_state(state)

    print("\n" + "=" * 66)
    print("Paste these into main.py:\n")
    print(f'GATEWAY_URL = "{state.get("gateway_url")}"')
    print(f'KB_ID       = "<kbid>"   # leave as-is until the KB phase')
    print(f'REGION      = "{REGION}"')
    print(f'MEMORY_ID   = "{state.get("memory_id")}"')
    print("=" * 66)
    print(f"\nBackend URL for a direct curl check:\n  {state['api']['invoke_url']}/orders/ORD-001")
    print("\nNext: verify the Gateway advertises 4 tools before deploying.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--inspect", action="store_true",
                   help="print AgentCore control-plane API shapes and exit")
    p.add_argument("--fix-runtime-role", metavar="ROLE_NAME",
                   help="attach the runtime execution policy (E8) after deploy")
    a = p.parse_args()

    try:
        if a.inspect:
            inspect_api()
        elif a.fix_runtime_role:
            fix_runtime_role(a.fix_runtime_role)
        else:
            build()
    except ClientError as e:
        print(f"\nAWS error: {e}", file=sys.stderr)
        print("Re-run the script — every step is idempotent and it will resume.",
              file=sys.stderr)
        sys.exit(1)