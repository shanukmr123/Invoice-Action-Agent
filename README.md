# Invoice Action Agent

FastAPI starter for the supplied A2A invoice-agent challenge.

## Deploy
1. Push this folder to a GitHub repository.
2. Create a Render Web Service from the repo; use the included `render.yaml`.
3. Attach persistent storage at `/data`.
4. Set `OPENAI_API_KEY` as a secret.
5. After Render assigns a hostname, set `APP_BASE_URL` to `https://HOSTNAME/a2a` and redeploy.
6. Submit `https://HOSTNAME/a2a` as the base URL. Agent Card: `https://HOSTNAME/.well-known/agent-card.json`.

## Local run
```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...
export APP_BASE_URL=http://localhost:8080/a2a
uvicorn app:app --port 8080
```

Authenticated endpoints require:
`Authorization: Bearer <principal-token>`,
`A2A-Version: 1.0`,
`Content-Type: application/a2a+json`.

## Safety behavior
Initial batches create proposals only. No payment is initiated. Continuations must match stored task/context/proposal/action IDs. Only accepted results generate receipt entries; rejected actions remain in history. SQLite WAL and immediate transactions serialize task transitions and cancellation. Idempotency keys are scoped to Bearer principal + messageId, and hashes cover the message only.

## Important pre-submission validation
The provided challenge excerpt does not include the full nested package/document schema, exact examples of task ProtoJSON fields, or all error-envelope conventions. The prompt assumes each package includes readable extracted document text and bracketed evidence references. If attachments are binary, add OCR/text extraction before the model call. Confirm task state serialization against the live harness; this starter emits `input-required`, `completed`, and `canceled`. The implementation is a proposal/receipt lifecycle and does not connect to a bank or payment rail.
