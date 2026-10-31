import os,json,uuid,hashlib,sqlite3,threading
from datetime import datetime,timezone
from fastapi import FastAPI,Request,Header,HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI

DB=os.getenv("DB_PATH","/data/agent.sqlite3")
BASE=os.getenv("APP_BASE_URL","https://YOUR-HOST/a2a")
KEY=os.getenv("OPENAI_API_KEY")
MODEL=os.getenv("OPENAI_MODEL","gpt-4.1-mini")
BATCH="application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL="application/vnd.ga5.invoice-action-proposals+json"
RESULT="application/vnd.ga5.invoice-action-results+json"
RECEIPT="application/vnd.ga5.invoice-action-receipts+json"
ACTIONS={"settle_invoice","request_approval","hold_invoice","reject_duplicate","open_exception"}
app=FastAPI(title="Invoice Action Agent",version="1.0.0")
lock=threading.RLock()

def db():
    os.makedirs(os.path.dirname(DB) or ".",exist_ok=True)
    c=sqlite3.connect(DB,timeout=30,isolation_level=None,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c
with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,principal TEXT,context_id TEXT,batch_id TEXT,state TEXT,body TEXT,created TEXT);
    CREATE INDEX IF NOT EXISTS owner_idx ON tasks(principal,created);
    CREATE TABLE IF NOT EXISTS msgs(principal TEXT,message_id TEXT,digest TEXT,task_id TEXT,PRIMARY KEY(principal,message_id));
    CREATE TABLE IF NOT EXISTS cache(hash TEXT PRIMARY KEY,proposal TEXT);
    """)

def digest(x): return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
def principal(auth):
    if not auth or not auth.startswith("Bearer ") or not auth[7:].strip(): raise HTTPException(401,"Unauthorized")
    return auth[7:].strip()
def headers(req):
    if req.headers.get("A2A-Version")!="1.0": raise HTTPException(400,"Unsupported version")
    if "application/a2a+json" not in req.headers.get("content-type",""): raise HTTPException(415,"Unsupported media type")
def data_part(msg,media):
    for p in msg.get("parts",[]):
        if p.get("mediaType")==media and isinstance(p.get("data"),dict): return p["data"]
def part(media,data): return {"mediaType":media,"data":data}
def validate(p):
    assert p["action"] in ACTIONS
    f=p["facts"]
    assert all(k in f for k in ("vendorName","invoiceNumber","amountMinor","currency"))
    assert isinstance(f["amountMinor"],int) and f["amountMinor"]>=0
    refs=p["evidenceRefs"]
    assert len(refs)==3 and all(x.startswith("[") and x.endswith("]") for x in refs)
    assert 60<=len(p["rationale"])<=1500 and all(x in p["rationale"] for x in refs)

def decide(packages):
    result=[None]*len(packages); todo=[]; positions=[]
    with db() as c:
        for i,pkg in enumerate(packages):
            h=digest(pkg); row=c.execute("SELECT proposal FROM cache WHERE hash=?",(h,)).fetchone()
            if row:
                p=json.loads(row[0]); p["packageId"]=pkg.get("packageId",pkg.get("id")); p["actionId"]=str(uuid.uuid4()); result[i]=p
            else: todo.append(pkg); positions.append((i,h))
    if todo:
        if not KEY: raise HTTPException(503,"AI provider not configured")
        prompt="""Analyze each invoice package independently. Return ONLY JSON {"proposals":[...]} with one object per package:
{"packageId":"...","action":"settle_invoice|request_approval|hold_invoice|reject_duplicate|open_exception","facts":{"vendorName":"...","invoiceNumber":"...","amountMinor":123,"currency":"INR"},"evidenceRefs":["[REF1]","[REF2]","[REF3]"],"rationale":"60-1500 chars"}.
settle_invoice only if valid, reconciled and within authority. request_approval if valid but outside delegated authority. hold_invoice if a stated verification must complete. reject_duplicate only if the SAME commercial invoice was already paid; a different invoice is not duplicate. open_exception for material conflicts. Cite exactly the three decisive bracketed refs in the action-determining paragraph; never cite cover sheets, archive examples or decoys. Do not infer absent facts. Amount in integer minor units. Packages:\n"""+json.dumps(todo,ensure_ascii=False)
        try:
            cli=OpenAI(api_key=KEY)
            r=cli.chat.completions.create(model=MODEL,temperature=0,response_format={"type":"json_object"},messages=[{"role":"user","content":prompt}],timeout=35)
            proposals=json.loads(r.choices[0].message.content)["proposals"]
            if len(proposals)!=len(todo): raise ValueError("wrong count")
            with db() as c:
                for (i,h),p,pkg in zip(positions,proposals,todo):
                    p["packageId"]=pkg.get("packageId",pkg.get("id",p.get("packageId")))
                    p["actionId"]=str(uuid.uuid4()); validate(p)
                    c.execute("INSERT OR REPLACE INTO cache VALUES(?,?)",(h,json.dumps({k:v for k,v in p.items() if k!="actionId"})))
                    result[i]=p
        except Exception as e: raise HTTPException(502,"Invoice analysis failed") from e
    ids=[p["packageId"] for p in result]
    if len(set(ids))!=len(ids): raise HTTPException(502,"Duplicate package IDs")
    return result

@app.get("/.well-known/agent-card.json")
def card():
    return {"name":"Invoice Action Agent","description":"Evidence-based invoice triage with approval-gated execution and durable state.","version":"1.0.0","capabilities":{},"supportedInterfaces":[{"url":BASE,"protocolBinding":"HTTP+JSON","protocolVersion":"1.0"}],"defaultInputModes":[BATCH,RESULT],"defaultOutputModes":[PROPOSAL,RECEIPT],"skills":[{"id":"invoice_action_agent","name":"Invoice Action Agent","description":"Analyze invoice packages and issue receipts only for accepted proposals.","tags":["invoice","accounts-payable","reconciliation","A2A"]}]}

@app.post("/a2a/message:send")
async def send(req:Request,authorization:str=Header(default=None)):
    headers(req); who=principal(authorization); body=await req.json(); msg=body.get("message",{})
    mid=msg.get("messageId")
    if not mid: raise HTTPException(400,"messageId required")
    h=digest(msg)
    with lock,db() as c:
        c.execute("BEGIN IMMEDIATE")
        old=c.execute("SELECT digest,task_id FROM msgs WHERE principal=? AND message_id=?",(who,mid)).fetchone()
        if old:
            if old["digest"]!=h: c.execute("ROLLBACK"); raise HTTPException(409,"IDEMPOTENCY_CONFLICT")
            row=c.execute("SELECT body FROM tasks WHERE id=?",(old["task_id"],)).fetchone(); c.execute("COMMIT")
            return {"task":json.loads(row["body"])}
        tid=msg.get("taskId")
        if tid:
            row=c.execute("SELECT * FROM tasks WHERE id=? AND principal=?",(tid,who)).fetchone()
            if not row: c.execute("ROLLBACK"); raise HTTPException(404,"Not found")
            task=json.loads(row["body"]); payload=data_part(msg,RESULT)
            if msg.get("contextId")!=task["contextId"] or not payload or task["status"]["state"]!="input-required":
                c.execute("ROLLBACK"); raise HTTPException(409,"Invalid continuation")
            proposals=task["artifacts"][0]["parts"][0]["data"]["proposals"]; byid={p["packageId"]:p for p in proposals}
            results=payload.get("results",[])
            if len(results)!=len(proposals): c.execute("ROLLBACK"); raise HTTPException(400,"Incomplete results")
            seen=set(); executions=[]
            for r in results:
                p=byid.get(r.get("packageId"))
                if not p or r["packageId"] in seen or r.get("actionId")!=p["actionId"] or r.get("action")!=p["action"]:
                    c.execute("ROLLBACK"); raise HTTPException(409,"Proposal mismatch")
                seen.add(r["packageId"])
                if r.get("outcome")=="ACCEPTED":
                    if not r.get("receiptNonce"): c.execute("ROLLBACK"); raise HTTPException(400,"Missing receipt")
                    executions.append({"packageId":p["packageId"],"actionId":p["actionId"],"action":p["action"],"receiptNonce":r["receiptNonce"],"facts":p["facts"],"evidenceRefs":p["evidenceRefs"]})
                elif r.get("outcome")!="REJECTED": c.execute("ROLLBACK"); raise HTTPException(400,"Invalid outcome")
            task["history"].append(msg); task["status"]={"state":"completed"}
            task["artifacts"].append({"parts":[part(RECEIPT,{"batchId":payload["batchId"],"executions":executions})]})
            c.execute("UPDATE tasks SET state='completed',body=? WHERE id=?",(json.dumps(task),tid))
            c.execute("INSERT INTO msgs VALUES(?,?,?,?)",(who,mid,h,tid)); c.execute("COMMIT")
            return {"task":task}
        batch=data_part(msg,BATCH)
        if not batch or not batch.get("batchId") or not isinstance(batch.get("packages"),list):
            c.execute("ROLLBACK"); raise HTTPException(400,"Invalid batch")
        c.execute("COMMIT")
    proposals=decide(batch["packages"])
    tid,cid=str(uuid.uuid4()),str(uuid.uuid4())
    task={"id":tid,"contextId":cid,"status":{"state":"input-required"},"history":[msg],"artifacts":[{"parts":[part(PROPOSAL,{"batchId":batch["batchId"],"proposals":proposals})]}],"kind":"task"}
    with lock,db() as c:
        c.execute("BEGIN IMMEDIATE")
        old=c.execute("SELECT digest,task_id FROM msgs WHERE principal=? AND message_id=?",(who,mid)).fetchone()
        if old:
            c.execute("ROLLBACK")
            if old["digest"]!=h: raise HTTPException(409,"IDEMPOTENCY_CONFLICT")
            with db() as d: return {"task":json.loads(d.execute("SELECT body FROM tasks WHERE id=?",(old["task_id"],)).fetchone()[0])}
        c.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",(tid,who,cid,batch["batchId"],"input-required",json.dumps(task),datetime.now(timezone.utc).isoformat()))
        c.execute("INSERT INTO msgs VALUES(?,?,?,?)",(who,mid,h,tid)); c.execute("COMMIT")
    return {"task":task}

@app.get("/a2a/tasks")
def tasks(req:Request,authorization:str=Header(default=None)):
    headers(req); who=principal(authorization)
    with db() as c: rows=c.execute("SELECT body FROM tasks WHERE principal=? ORDER BY created DESC",(who,)).fetchall()
    return {"tasks":[json.loads(x[0]) for x in rows]}

@app.get("/a2a/tasks/{tid}")
def get_task(tid:str,req:Request,authorization:str=Header(default=None)):
    headers(req); who=principal(authorization)
    with db() as c: row=c.execute("SELECT body FROM tasks WHERE id=? AND principal=?",(tid,who)).fetchone()
    if not row: raise HTTPException(404,"Not found")
    return json.loads(row[0])

@app.post("/a2a/tasks/{tid}:cancel")
def cancel(tid:str,req:Request,authorization:str=Header(default=None)):
    headers(req); who=principal(authorization)
    with lock,db() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT body,state FROM tasks WHERE id=? AND principal=?",(tid,who)).fetchone()
        if not row: c.execute("ROLLBACK"); raise HTTPException(404,"Not found")
        if row["state"] in ("completed","canceled"): c.execute("ROLLBACK"); raise HTTPException(409,"Terminal task")
        task=json.loads(row["body"]); task["status"]={"state":"canceled"}
        task["artifacts"]=[a for a in task["artifacts"] if a["parts"][0]["mediaType"]!=RECEIPT]
        c.execute("UPDATE tasks SET state='canceled',body=? WHERE id=?",(json.dumps(task),tid)); c.execute("COMMIT")
    return task

@app.get("/health")
def health(): return {"ok":True}

@app.exception_handler(HTTPException)
async def errors(req,exc): return JSONResponse(status_code=exc.status_code,content={"error":str(exc.detail)})
