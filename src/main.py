"""
Sidak's job-bot: scrape → score → tailor resume → compile PDF → upload → email digest.
Runs daily via GitHub Actions. Free-tier only.

Stack:
  - JobSpy (scrape Naukri/Indeed/LinkedIn)
  - Gemini 2.0 Flash (free tier: 1500 req/day)
  - Tectonic (LaTeX → PDF, runs in CI)
  - Google Drive API (storage)
  - Gmail SMTP (digest email)
"""
import json
import os
import re
import smtplib
import subprocess
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import google.generativeai as genai
import jinja2
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from jobspy import scrape_jobs

# ---------- Config ----------
ROOT = Path(__file__).parent.parent
PROFILE_PATH = ROOT / "config" / "sidak_profile.json"
TEMPLATE_PATH = ROOT / "templates" / "resume.tex.j2"
PROMPT_PATH = ROOT / "src" / "rewrite_prompt.md"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

SEARCH_TERMS = ["data analyst", "business analyst", "BI analyst", "analytics analyst"]
LOCATIONS = ["Bangalore, India", "Hyderabad, India", "Gurgaon, India",
             "Mumbai, India", "Pune, India", "Remote, India"]
HOURS_OLD = 24
RESULTS_PER_SEARCH = 15
MIN_ATS_SCORE = 60

# Hard cap on Gemini calls per run. Free tier on 2.0-flash is 1500/day, but
# we cap aggressively so a bug can never cost real money.
MAX_GEMINI_CALLS = 60
_gemini_call_count = 0


def safe_str(value, max_len: int | None = None) -> str:
    """Convert any field (incl. NaN floats from pandas) to a clean string."""
    if value is None:
        return ""
    # pandas NaN: float that isn't equal to itself
    if isinstance(value, float) and value != value:
        return ""
    s = str(value).strip()
    if max_len:
        s = s[:max_len]
    return s

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GOOGLE_OAUTH_TOKEN_JSON = os.environ["GOOGLE_OAUTH_TOKEN_JSON"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL)


# ---------- 1. Scrape ----------
def scrape_all() -> list[dict]:
    all_jobs = []
    for term in SEARCH_TERMS:
        for loc in LOCATIONS:
            try:
                df = scrape_jobs(
                    site_name=["naukri", "indeed", "linkedin"],
                    search_term=term,
                    location=loc,
                    results_wanted=RESULTS_PER_SEARCH,
                    hours_old=HOURS_OLD,
                    country_indeed="india",
                )
                if df is not None and len(df) > 0:
                    all_jobs.extend(df.to_dict("records"))
                time.sleep(2)  # gentle on the scrapers
            except Exception as e:
                print(f"[scrape] {term} @ {loc}: {e}")
    # dedupe on (company, title)
    seen = set()
    unique = []
    for j in all_jobs:
        key = (safe_str(j.get("company")).lower(),
               safe_str(j.get("title")).lower())
        if key in seen or not key[0]:
            continue
        seen.add(key)
        unique.append(j)
    print(f"[scrape] {len(unique)} unique jobs")
    return unique


# ---------- 2 + 3. Score & rewrite (single Gemini call per job) ----------
def tailor(profile: dict, job: dict, prompt_template: str) -> dict | None:
    global _gemini_call_count
    if _gemini_call_count >= MAX_GEMINI_CALLS:
        print(f"[tailor] hit MAX_GEMINI_CALLS={MAX_GEMINI_CALLS}, stopping")
        return None

    description = safe_str(job.get("description"), max_len=4000)
    user_msg = (
        f"# PROFILE\n```json\n{json.dumps(profile, indent=2)}\n```\n\n"
        f"# JOB\n"
        f"Title: {safe_str(job.get('title'))}\n"
        f"Company: {safe_str(job.get('company'))}\n"
        f"Location: {safe_str(job.get('location'))}\n"
        f"Description:\n{description}"
    )
    try:
        _gemini_call_count += 1
        resp = model.generate_content(
            [prompt_template, user_msg],
            generation_config={"response_mime_type": "application/json", "temperature": 0.3},
        )
        return json.loads(resp.text)
    except Exception as e:
        msg = str(e)
        # If we hit a hard quota error, abort the whole run — no point in burning more calls.
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
            print(f"[tailor] quota error, aborting remaining jobs: {msg[:200]}")
            _gemini_call_count = MAX_GEMINI_CALLS  # hard stop
        else:
            print(f"[tailor] failed for {safe_str(job.get('company'))}: {msg[:200]}")
        return None


# ---------- 4. Render LaTeX & compile PDF ----------
def render_latex(profile: dict, tailored: dict) -> str:
    env = jinja2.Environment(
        block_start_string="((*", block_end_string="*))",
        variable_start_string="((*", variable_end_string="*))",
        comment_start_string="((#", comment_end_string="#))",
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    # Single delimiter style — switch to standard Jinja for clarity
    env = jinja2.Environment(
        variable_start_string="((*", variable_end_string="*))",
        block_start_string="((%", block_end_string="%))",
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    # Re-read template with the right delim — simpler: use plain Jinja {{ }} but escape in TeX
    # We'll use a string.Template-style fill below to keep it bulletproof:
    tex = TEMPLATE_PATH.read_text()

    p = profile["personal"]
    edu = profile["education"]

    # Header & summary
    tex = tex.replace("((* PERSONAL_NAME *))", latex_escape(p["name"]))
    tex = tex.replace("((* PERSONAL_PHONE *))", latex_escape(p["phone"]))
    tex = tex.replace("((* PERSONAL_EMAIL *))", latex_escape(p["email"]))
    tex = tex.replace("((* PERSONAL_LINKEDIN *))", p.get("linkedin", "#"))
    tex = tex.replace("((* PERSONAL_GITHUB *))", p.get("github", "#"))
    tex = tex.replace("((* PERSONAL_LEETCODE *))", p.get("leetcode", "#"))
    tex = tex.replace("((* TAILORED_SUMMARY *))", latex_escape(tailored["tailored_summary"]))

    # Skills
    sk = tailored["skills_ordered"]
    tex = tex.replace("((* SKILLS_DATA_ANALYSIS *))", latex_escape(", ".join(sk["data_analysis"])))
    tex = tex.replace("((* SKILLS_BI_REPORTING *))", latex_escape(", ".join(sk["bi_reporting"])))
    tex = tex.replace("((* SKILLS_TOOLS *))", latex_escape(", ".join(sk["tools"])))

    # Experience block
    exp_block = ""
    for exp in tailored["experience"]:
        client_str = f" $|$ Client: {latex_escape(exp.get('client',''))}" if exp.get("client") else ""
        exp_block += (
            "  \\resumeSubheading\n"
            f"    {{{latex_escape(exp['company'])}{client_str}}}{{{latex_escape(exp['dates'])}}}\n"
            f"    {{{latex_escape(exp['title'])}}}{{{latex_escape(exp['location'])}}}\n"
            "    \\resumeItemListStart\n"
        )
        for b in exp["bullets"]:
            exp_block += f"      \\resumeItem{{{latex_escape(b['rewritten'])}}}\n"
        exp_block += "    \\resumeItemListEnd\n"
    # Replace the whole templated experience loop with rendered block
    tex = re.sub(
        r"\(\(\* FOR exp IN EXPERIENCE \*\)\).*?\(\(\* ENDFOR \*\)\)",
        exp_block.replace("\\", r"\\"),
        tex, flags=re.DOTALL,
    )

    # Projects block
    proj_block = ""
    for pr in tailored["projects"]:
        proj_block += (
            "  \\resumeProjectHeading\n"
            f"    {{\\textbf{{{latex_escape(pr['name'])}}} $|$ \\emph{{{latex_escape(pr['tools_line'])}}}}}{{{latex_escape(pr['dates'])}}}\n"
            "    \\resumeItemListStart\n"
        )
        for b in pr["bullets"]:
            proj_block += f"      \\resumeItem{{{latex_escape(b['rewritten'])}}}\n"
        proj_block += "    \\resumeItemListEnd\n"
    tex = re.sub(
        r"\(\(\* FOR p IN PROJECTS \*\)\).*?\(\(\* ENDFOR \*\)\)",
        proj_block.replace("\\", r"\\"),
        tex, flags=re.DOTALL,
    )

    # Education
    tex = tex.replace("((* EDU_INSTITUTE *))", latex_escape(edu["institute"]))
    tex = tex.replace("((* EDU_DATES *))", latex_escape(f"{edu['start']} – {edu['end']}"))
    tex = tex.replace("((* EDU_DEGREE *))", latex_escape(edu["degree"]))
    tex = tex.replace("((* EDU_CGPA *))", latex_escape(edu["cgpa"]))
    tex = tex.replace("((* EDU_LOCATION *))", latex_escape(edu["location"]))

    # Certifications (one line, pipe-separated)
    certs = "  $|$  ".join(latex_escape(c) for c in profile["certifications"])
    tex = tex.replace("((* CERTIFICATIONS *))", certs)

    return tex


def latex_escape(s: str) -> str:
    """Escape LaTeX special chars."""
    if not isinstance(s, str):
        s = str(s)
    repl = {
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    return s


def compile_pdf(tex_source: str, out_path: Path) -> bool:
    """Compile via tectonic (installed in GH Actions runner)."""
    tex_path = out_path.with_suffix(".tex")
    tex_path.write_text(tex_source)
    try:
        subprocess.run(
            ["tectonic", "-X", "compile", str(tex_path), "--outdir", str(out_path.parent)],
            check=True, capture_output=True, timeout=60,
        )
        return out_path.exists()
    except subprocess.CalledProcessError as e:
        print(f"[tectonic] {e.stderr.decode()[:500]}")
        return False


# ---------- 5. Drive upload ----------
def get_drive_service():
    creds_data = json.loads(GOOGLE_OAUTH_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(creds_data)
    return build("drive", "v3", credentials=creds)


def upload_to_drive(service, file_path: Path, name: str) -> str:
    metadata = {"name": name, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(str(file_path), mimetype="application/pdf")
    f = service.files().create(body=metadata, media_body=media, fields="id,webViewLink").execute()
    service.permissions().create(
        fileId=f["id"], body={"role": "reader", "type": "anyone"},
    ).execute()
    return f["webViewLink"]


# ---------- 6. Email digest ----------
def send_digest(rows: list[dict]):
    if not rows:
        body = "<p>No matching jobs found in the last 24 hours.</p>"
    else:
        body = "<h2>Today's tailored applications</h2><table border='1' cellpadding='6' style='border-collapse:collapse'><tr><th>Score</th><th>Company</th><th>Role</th><th>Location</th><th>Job</th><th>Resume</th></tr>"
        for r in rows:
            body += (
                f"<tr><td>{r['score']}</td><td>{r['company']}</td><td>{r['title']}</td>"
                f"<td>{r['location']}</td>"
                f"<td><a href='{r['job_url']}'>Apply</a></td>"
                f"<td><a href='{r['pdf_url']}'>Tailored PDF</a></td></tr>"
            )
        body += "</table>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[JobBot] {len(rows)} tailored applications ready"
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_USER
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)


# ---------- Main ----------
def main():
    profile = json.loads(PROFILE_PATH.read_text())
    prompt = PROMPT_PATH.read_text()

    jobs = scrape_all()
    if not jobs:
        send_digest([])
        return

    drive = get_drive_service()
    digest_rows = []
    for j in jobs:
        tailored = tailor(profile, j, prompt)
        if not tailored:
            continue
        if not tailored.get("should_apply") or tailored.get("ats_match_score", 0) < MIN_ATS_SCORE:
            print(f"[skip] {safe_str(j.get('company'))} — score {tailored.get('ats_match_score')}")
            continue

        safe_company = re.sub(r"\W+", "_", safe_str(j.get("company")) or "co")[:40]
        pdf_path = OUTPUT_DIR / f"sidak_{safe_company}_{int(time.time())}.pdf"
        tex = render_latex(profile, tailored)
        if not compile_pdf(tex, pdf_path):
            continue

        try:
            pdf_url = upload_to_drive(drive, pdf_path, pdf_path.name)
        except Exception as e:
            print(f"[drive] upload failed: {e}")
            continue

        digest_rows.append({
            "score": tailored["ats_match_score"],
            "company": safe_str(j.get("company")),
            "title": safe_str(j.get("title")),
            "location": safe_str(j.get("location")),
            "job_url": safe_str(j.get("job_url")),
            "pdf_url": pdf_url,
        })
        time.sleep(2)  # respect Gemini free-tier RPM (15 RPM = 1 every 4s, we go faster but safer than 1s)

    digest_rows.sort(key=lambda r: r["score"], reverse=True)
    send_digest(digest_rows)
    print(f"[done] {len(digest_rows)} applications prepared")


if __name__ == "__main__":
    main()
