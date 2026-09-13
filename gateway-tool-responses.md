# Gateway Tool Responses — raw payloads

Captured: 2026-09-13T14:57:53  
Method: direct MCP session to the AgentCore Gateway. No model, no agent, no memory — so each payload below is exactly what the Gateway returned.


**Result:** 7 of 7 valid calls returned a non-empty, well-formed payload.


## Tools advertised (6)

- `order-tracker___get_customer`
- `order-tracker___get_customer_orders`
- `order-tracker___get_order`
- `refund-processor___check_refund_status`
- `refund-processor___get_return_label`
- `refund-processor___initiate_refund`

### Order lookup — shipped order

**Tool:** `order-tracker___get_order`  
**Arguments:** `{"order_id": "ORD-001"}`  
**Verdict:** OK — non-empty, valid structured JSON

```json
{
  "order_id": "ORD-001",
  "customer_id": "CUST-123",
  "status": "SHIPPED",
  "items": [
    {
      "name": "Wireless Headphones Pro",
      "qty": 1,
      "price": 89.99
    }
  ],
  "total": 89.99,
  "tracking_number": "TRK987654321",
  "carrier": "UPS",
  "estimated_delivery": "2026-09-15"
}
```


### Order lookup — delivered order

**Tool:** `order-tracker___get_order`  
**Arguments:** `{"order_id": "ORD-002"}`  
**Verdict:** OK — non-empty, valid structured JSON

```json
{
  "order_id": "ORD-002",
  "customer_id": "CUST-123",
  "status": "DELIVERED",
  "items": [
    {
      "name": "Kindle Paperwhite",
      "qty": 1,
      "price": 139.99
    }
  ],
  "total": 139.99,
  "tracking_number": "TRK123456789",
  "carrier": "USPS",
  "delivered_date": "2026-09-10"
}
```


### Customer profile — tier and points

**Tool:** `order-tracker___get_customer`  
**Arguments:** `{"customer_id": "CUST-123"}`  
**Verdict:** OK — non-empty, valid structured JSON

```json
{
  "name": "Jane Smith",
  "loyalty_points": 4250,
  "tier": "Gold"
}
```


### All orders for a customer

**Tool:** `order-tracker___get_customer_orders`  
**Arguments:** `{"customer_id": "CUST-123"}`  
**Verdict:** OK — non-empty, valid structured JSON

```json
{
  "customer_id": "CUST-123",
  "orders": [
    {
      "order_id": "ORD-001",
      "customer_id": "CUST-123",
      "status": "SHIPPED",
      "items": [
        {
          "name": "Wireless Headphones Pro",
          "qty": 1,
          "price": 89.99
        }
      ],
      "total": 89.99,
      "tracking_number": "TRK987654321",
      "carrier": "UPS",
      "estimated_delivery": "2026-09-15"
    },
    {
      "order_id": "ORD-002",
      "customer_id": "CUST-123",
      "status": "DELIVERED",
      "items": [
        {
          "name": "Kindle Paperwhite",
          "qty": 1,
          "price": 139.99
        }
      ],
      "total": 139.99,
      "tracking_number": "TRK123456789",
      "carrier": "USPS",
      "delivered_date": "2026-09-10"
    }
  ]
}
```


### Refund initiation

**Tool:** `refund-processor___initiate_refund`  
**Arguments:** `{"order_id": "ORD-002", "reason": "damaged on arrival", "amount": 139.99}`  
**Verdict:** OK — non-empty, valid structured JSON

```json
{
  "statusCode": 200,
  "body": "{\"refund_id\": \"REF-6JB6DISO\", \"order_id\": \"ORD-002\", \"status\": \"APPROVED\", \"amount\": 139.99, \"message\": \"Refund approved. Credit appears in 3-5 business days.\", \"created_at\": \"2026-09-13T14:57:55.461416\"}"
}
```


### Prepaid return label

**Tool:** `refund-processor___get_return_label`  
**Arguments:** `{"order_id": "ORD-002"}`  
**Verdict:** OK — non-empty, valid structured JSON

```json
{
  "statusCode": 200,
  "body": "{\"order_id\": \"ORD-002\", \"label_url\": \"https://returns.amazon.com/label/ORD-002\", \"carrier\": \"UPS\", \"valid_until\": \"2025-12-31\"}"
}
```


### Refund status lookup

**Tool:** `refund-processor___check_refund_status`  
**Arguments:** `{"refund_id": "REF-TEST0001"}`  
**Verdict:** OK — non-empty, valid structured JSON

```json
{
  "statusCode": 200,
  "body": "{\"refund_id\": \"REF-TEST0001\", \"status\": \"PROCESSING\", \"eta\": \"2-3 business days\"}"
}
```


### NEGATIVE CONTROL — non-existent order ID

**Tool:** `order-tracker___get_order`  
**Arguments:** `{"order_id": "ORD-999"}`  
**Verdict:** ERROR PATH (expected for the negative control)

```json
An internal error occurred. Please retry later.
```
