import httpx
import json
import pandas as pd
from pathlib import Path
from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text, TIMESTAMP, func
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from openai import OpenAI

# Config
SLACK_BOT_TOKEN = "xoxb-your-slack-bot-token"
SLACK_CHANNEL = "#incidents"
GITHUB_TOKEN = "github_pat_your-github-token"
GITHUB_REPO = "aaryanb090/incident-tracker"
DATABASE_URL = "postgresql://username:password@localhost:5432/incidentdb"
KB_FILE = Path("kb.csv")
kb_df = None
kb_vectorizer = None
kb_matrix = None
KB_ENABLED = False

# Database
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class Incident(Base):
    __tablename__ = "incidents"
    id = Column(Integer, primary_key=True, index=True)
    incident_type = Column(String(100), nullable=False)
    description = Column(Text, nullable=False)
    severity = Column(String(20), nullable=False)
    remediation = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

Base.metadata.create_all(bind=engine)

# Knowledge Base
def load_kb():
    global kb_df, kb_vectorizer, kb_matrix, KB_ENABLED
    if not KB_FILE.exists():
        print("kb.csv not found; KB disabled")
        KB_ENABLED = False
        return
    df = pd.read_csv(KB_FILE)
    if not {"problem", "remediation"}.issubset(set(df.columns)):
        print("kb.csv must have columns: problem, remediation")
        KB_ENABLED = False
        return
    df["problem"] = df["problem"].astype(str).str.lower()
    vec = TfidfVectorizer()
    mat = vec.fit_transform(df["problem"])
    kb_df, kb_vectorizer, kb_matrix, KB_ENABLED = df, vec, mat, True
    print(f"KB loaded with {len(kb_df)} entries")

def kb_lookup(text: str, threshold: float = 0.50):
    if not KB_ENABLED:
        return None, 0.0
    q = kb_vectorizer.transform([str(text).lower()])
    sims = cosine_similarity(q, kb_matrix)[0]
    idx = sims.argmax()
    score = float(sims[idx])
    if score >= threshold:
        return kb_df.iloc[idx]["remediation"], score
    return None, score

load_kb()

# External Integrations
def send_slack_message(text: str):
    url = "https://slack.com/api/chat.postMessage"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    payload = {"channel": SLACK_CHANNEL, "text": text}
    with httpx.Client() as client:
        r = client.post(url, json=payload, headers=headers)
        print("Slack response:", r.status_code, r.text)
        return r.json().get("ok", False)

def create_github_issue(title: str, body: str):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    payload = {"title": title, "body": body}
    with httpx.Client() as client:
        r = client.post(url, json=payload, headers=headers)
        if r.status_code in (200, 201):
            return r.json().get("html_url")
        print("GitHub issue creation failed:", r.text)
        return None

# OpenAI
client = OpenAI(api_key="sk-your-openai-api-key")

def classify_severity_llm(description: str) -> str:
    prompt = f"""Classify the severity of this incident as exactly one of: High, Medium, Low.
Incident: {description}
Respond with only one word: High, Medium, or Low."""
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=5,
        temperature=0
    )
    return r.choices[0].message.content.strip()

def classify_and_remediate(description: str):
    prompt = f"""You are an incident management assistant.
Classify severity as strictly one of: "High", "Medium", "Low".
Suggest a short remediation step.

Respond strictly in JSON like:
{{
  "severity": "High|Medium|Low",
  "remediation": "short fix"
}}

Incident: {description}"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=100,
        temperature=0
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").replace("json", "").strip()
    return json.loads(raw)

# FastAPI
app = FastAPI()

class IncidentIn(BaseModel):
    incident_type: str
    description: str

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/")
def root():
    return {"message": "Incident API is running"}

@app.post("/incident")
def create_incident(payload: IncidentIn, db: Session = Depends(get_db)):
    kb_remediation, kb_score = kb_lookup(payload.description)

    if kb_remediation:
        severity = classify_severity_llm(payload.description)
        remediation = kb_remediation
        used_kb = True
    else:
        llm = classify_and_remediate(payload.description)
        severity, remediation, used_kb = llm["severity"], llm["remediation"], False

    new_incident = Incident(
        incident_type=payload.incident_type,
        description=payload.description,
        severity=severity,
        remediation=remediation
    )
    db.add(new_incident)
    db.commit()
    db.refresh(new_incident)

    title = f"[INC-{new_incident.id}] {new_incident.incident_type} ({new_incident.severity})"
    body = f"""Incident ID: {new_incident.id}
Type: {new_incident.incident_type}
Severity: {new_incident.severity}
Description: {new_incident.description}
Remediation: {new_incident.remediation}"""
    issue_url = create_github_issue(title, body)

    slack_text = f"""Incident {new_incident.id}
Type: {new_incident.incident_type}
Severity: {new_incident.severity}
Description: {new_incident.description}
Remediation: {new_incident.remediation}
KB Match: {"Yes" if used_kb else "No"} (score: {kb_score:.2f})
GitHub Issue: {issue_url or 'N/A'}"""
    send_slack_message(slack_text)

    return {
        "id": new_incident.id,
        "incident_type": new_incident.incident_type,
        "description": new_incident.description,
        "severity": new_incident.severity,
        "remediation": new_incident.remediation,
        "created_at": new_incident.created_at,
        "github_issue": issue_url,
        "kb_used": used_kb,
        "kb_score": round(kb_score, 2)
    }

@app.get("/incidents")
def list_incidents(db: Session = Depends(get_db)):
    incidents = db.query(Incident).all()
    return [
        {
            "id": inc.id,
            "incident_type": inc.incident_type,
            "description": inc.description,
            "severity": inc.severity,
            "remediation": inc.remediation,
            "created_at": inc.created_at
        }
        for inc in incidents
    ]

@app.get("/incident/{incident_id}")
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        return {"error": f"Incident {incident_id} not found"}
    return {
        "id": incident.id,
        "incident_type": incident.incident_type,
        "description": incident.description,
        "severity": incident.severity,
        "remediation": incident.remediation,
        "created_at": incident.created_at
    }
