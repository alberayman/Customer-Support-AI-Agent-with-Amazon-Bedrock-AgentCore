# Reflection — Customer Support AI Agent (Bedrock AgentCore + Strands)

**Design decision.** I put the loyalty arithmetic in the AgentCore Code
Interpreter rather than letting the model compute it. Nova 2 Lite can usually
multiply, but "usually" is not a billing guarantee, and a wrong discount quoted to
a customer is an escalation. Running it through `code_session(REGION).invoke(
"executeCode", ...)` put the rules — 100 points to the dollar, a 500-point minimum
floored to the nearest 500, a 50% cap, tier rate applied to the post-points
subtotal — into code I can test. It paid off unexpectedly: on a $450 order the
sandbox returned a $41 tier discount while the model's prose called it "$45". The
final total it quoted was the sandbox's $369, because that figure came from the
tool. Had the arithmetic lived in the prompt, the wrong number would have been the
answer rather than a slip in the narration.

**Challenge.** The cross-session memory test failed and I nearly debugged a hook
that was fine. Instead of editing it, I queried the memory namespaces directly and
found the record — *"The user's name is Jane and she prefers concise responses"* —
created a few seconds **after** session B had run its retrieval. Extraction is
asynchronous; my 90-second wait lost the race by about ten seconds. Extending it to
180 fixed it. The same investigation exposed a real defect: the starter's
`actor_id` default pools every caller into one namespace, so earlier test runs had
written order history to the same actor, and session A volunteered details I had
not asked for. I changed the default to a per-session anonymous actor. That is a
data-isolation bug, not just a testing nuisance.

**Production consideration.** The Gateway uses the NONE authorizer and my runtime
policy grants actions on `"Resource": "*"`. Both were expedient; neither belongs in
production. The Gateway needs a JWT authorizer with a real issuer, and the role
should be scoped to the specific memory, gateway and knowledge-base ARNs it uses.
The sandbox made the point for me: the lab role could attach policies to itself,
which is how I unblocked a missing `s3vectors` permission — effective, and exactly
the privilege-escalation path an access review exists to catch. I would also put a
value threshold on the refund tool so no agent approves an unbounded return without
a human in the loop.
