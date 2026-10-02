"""Offline scoring with explicit neutral scores and stable blinded response IDs."""

import hashlib
import json
from pathlib import Path

from .report import _write_private
from .runner import MODELS

ANCHORS = {
    -3: "Unacceptable: seriously wrong, harmful, or fabricated personal facts",
    -2: "Clearly worse: robotic, pushy, repetitive, or out of character",
    -1: "Noticeable flaw; would prefer it stayed silent",
    0: "Acceptable but unremarkable, or appropriate silence",
    1: "Good, natural, relevant",
    2: "Very good; distinctly fits this friend",
    3: "Excellent; exactly the response you would want",
}


def scoring_data(cases: list[dict], records: list[dict], reveal: dict) -> dict:
    by_case = {case["case_id"]: {} for case in cases}
    if not isinstance(reveal, dict) or len(by_case) != len(cases) or set(by_case) != set(reveal):
        raise ValueError("Cases and reveal map must have the same unique case IDs.")
    for record in records:
        case_id, model = record["case_id"], record["model"]
        if case_id not in by_case or model not in MODELS or model in by_case[case_id]:
            raise ValueError("Unknown or duplicate result identity.")
        by_case[case_id][model] = record
    blind_cases = []
    for case in cases:
        case_id = case["case_id"]
        labels = reveal[case_id]
        if (not isinstance(labels, dict) or set(labels) != set("ABCD")
                or any(not isinstance(model, str) for model in labels.values())
                or set(labels.values()) != set(MODELS)):
            raise ValueError("Invalid four-way reveal map.")
        candidates = []
        for label in "ABCD":
            record = by_case[case_id].get(labels[label])
            parsed = record.get("parsed") if record else None
            valid = record is not None and record.get("status") == "ok" and isinstance(parsed, dict)
            decision = parsed.get("send" if case["kind"] == "initiate" else "respond") if valid else None
            if type(decision) is not bool:
                valid = False
            if record is None:
                status = "Not attempted; not a silence decision"
            elif not valid:
                status = "Format error; not a silence decision" if record.get("error_type") == "format" else "Request/completion error; not a silence decision"
            else:
                status = ("Initiate" if case["kind"] == "initiate" else "Reply") if decision else "Silence (explicit false)"
            text = ""
            memory = None
            if valid:
                messages = parsed.get("messages") or ([parsed["message"]] if parsed.get("message") else [])
                text = "\n\n".join(messages)
                memory = parsed.get("memory_update")
            elif record:
                text = record.get("raw", "")
            candidates.append({"id": f"{case_id}:{label}", "label": label,
                               "status": status, "text": text, "memory": memory,
                               "attempted": record is not None})
        blind_cases.append({"case_id": case_id, "friend": case["friend"], "kind": case["kind"],
                            "context": case.get("input_excerpt", ""),
                            "full_context": json.dumps(case["messages"], ensure_ascii=False, indent=2),
                            "candidates": candidates})
    # Bind exported scores to this exact letter map, ordering, context and output.
    # Hash includes the reveal map but the HTML never embeds model identities.
    identity = json.dumps({"reveal": reveal, "cases": blind_cases}, sort_keys=True, ensure_ascii=False)
    run_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {"version": 1, "run_id": run_id, "anchors": ANCHORS, "cases": blind_cases}


def summarize_scores(data: dict, reveal: dict, exported: dict) -> dict:
    if not isinstance(exported, dict) or exported.get("version") != 1 or exported.get("run_id") != data["run_id"]:
        raise ValueError("Score export belongs to a different comparison or letter map.")
    scores = exported.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("Score export must contain an identified score mapping.")
    eligible = {candidate["id"] for case in data["cases"] for candidate in case["candidates"] if candidate["attempted"]}
    if any(key not in eligible or type(value) is not int or not -3 <= value <= 3 for key, value in scores.items()):
        raise ValueError("Unknown response ID, unattempted response, or invalid score.")
    totals = {model: {"scored": 0, "sum": 0, "neutral": 0, "unscored": 0} for model in MODELS}
    matched = {model: [] for model in MODELS}
    complete_cases = 0
    for case in data["cases"]:
        complete = all(candidate["id"] in scores for candidate in case["candidates"])
        complete_cases += complete
        for candidate in case["candidates"]:
            model = reveal[case["case_id"]][candidate["label"]]
            aggregate = totals[model]
            if candidate["id"] not in scores:
                aggregate["unscored"] += 1
                continue
            value = scores[candidate["id"]]
            aggregate["scored"] += 1
            aggregate["sum"] += value
            aggregate["neutral"] += value == 0
            if complete:
                matched[model].append(value)
    for model, aggregate in totals.items():
        aggregate["mean"] = aggregate["sum"] / aggregate["scored"] if aggregate["scored"] else None
        aggregate["complete_case_mean"] = sum(matched[model]) / len(matched[model]) if matched[model] else None
    return {"complete_cases": complete_cases, "models": totals,
            "note": "Unscored is not zero. Use complete_case_mean for matched comparisons; unequal partial samples may be biased. Format reliability remains separate from subjective scores."}


def write_scorecard(output_dir: Path, cases: list[dict], records: list[dict], reveal: dict,
                    *, server_mode: bool = False) -> None:
    data = scoring_data(cases, records, reveal)
    data["summary_url"] = "/summary" if server_mode else None
    # Escaping '<' prevents private chat content from closing the data script tag.
    encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    _write_private(output_dir / "scorecard.html", _HTML.replace("__SCORING_DATA__", encoded))


_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; base-uri 'none'">
<title>Blind response scorecard</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f7;color:#17202a;font:16px/1.5 system-ui,sans-serif}main{max-width:1400px;margin:auto;padding:24px}h1{margin:0}header{display:flex;align-items:center;gap:16px;flex-wrap:wrap}button,select{font:inherit;padding:8px 12px;cursor:pointer}button:disabled{cursor:default;opacity:.5}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:18px 0}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card,.context,.rubric{background:white;border:1px solid #cbd2d9;border-radius:8px;padding:18px}.card h3{margin-top:0}.id{font:12px ui-monospace,monospace;color:#52606d;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.5 ui-monospace,monospace}select{width:100%;margin-top:12px}#notice{color:#8b2500}summary{cursor:pointer}.rubric{margin:16px 0}.rubric p{margin:4px 0}.status{font-weight:600}#position{min-width:150px}#context{max-height:320px;overflow:auto}.candidate-text{min-height:70px}.subtle{color:#52606d}@media(max-width:700px){main{padding:14px}.cards{grid-template-columns:1fr}}
</style></head><body><main>
<header><h1>Blind response scorecard</h1><strong id="progress"></strong></header>
<p>One overall score: <strong>Would I want this response in the group?</strong> A blank score means <strong>unscored</strong>, never zero. Letters change models between cases. No external network requests or model calls.</p>
<details class="rubric" open><summary><strong>−3 to +3 anchors</strong></summary><div id="anchors"></div></details>
<p class="subtle">Current memories may contain hindsight. Scheduling gates and memory validation are not tested. Score naturalness, personality, context, restraint and memory accuracy together. Format failures remain a separate reliability metric; you may still judge their raw text.</p>
<div class="toolbar"><button id="previous">Previous</button><strong id="position"></strong><button id="next">Next</button><button id="next-unscored">Next unfinished</button><button id="export">Export scores</button><button id="reveal" hidden>Reveal scored results</button><label>Restore exported scores <input id="import" type="file" accept="application/json,.json"></label></div>
<p id="notice" role="status"></p><h2 id="heading"></h2>
<div class="context"><details open><summary>Conversation excerpt</summary><pre id="context"></pre></details><details><summary>Full frozen prompt, personality and memory</summary><pre id="full-context"></pre></details></div>
<section id="cards" class="cards" aria-label="Candidates"></section>
<section id="results" hidden aria-label="Scored model results"></section>
</main><script id="comparison" type="application/json">__SCORING_DATA__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('comparison').textContent);
const key='sudomake-friends-scores:'+data.run_id;
const ids=new Set(data.cases.flatMap(c=>c.candidates.filter(r=>r.attempted).map(r=>r.id)));
let state={version:1,run_id:data.run_id,scores:{},current_case:0};
const el=id=>document.getElementById(id);
function validate(value){
 if(!value||value.version!==1||value.run_id!==data.run_id||!value.scores||typeof value.scores!=='object'||Array.isArray(value.scores))throw Error('Scores belong to another comparison or are malformed.');
 for(const [id,score] of Object.entries(value.scores))if(!ids.has(id)||!Number.isInteger(score)||score< -3||score>3)throw Error('Invalid response ID or score.');
 const index=value.current_case??0;
 if(!Number.isInteger(index)||index<0||index>=data.cases.length)throw Error('Invalid saved position.');
 return {version:1,run_id:data.run_id,scores:{...value.scores},current_case:index};
}
try{const saved=localStorage.getItem(key);if(saved)state=validate(JSON.parse(saved));}catch(error){el('notice').textContent='Saved progress could not be restored. Import an exported score file or start unscored.';}
function save(){try{localStorage.setItem(key,JSON.stringify(state));el('notice').textContent='Progress saved in this browser. Export a backup before closing or changing browsers.';}catch(error){el('notice').textContent='Browser storage is unavailable. Export scores before closing this page.';}}
for(let score=-3;score<=3;score++){const p=document.createElement('p');p.textContent=(score>0?'+':'')+score+' — '+data.anchors[String(score)];el('anchors').append(p);}
function hasScore(id){return Object.hasOwn(state.scores,id);}
function complete(c){return c.candidates.filter(r=>r.attempted).every(r=>hasScore(r.id));}
function render(){
 const c=data.cases[state.current_case];el('progress').textContent=Object.keys(state.scores).length+' / '+ids.size+' scored';
 el('position').textContent='Case '+(state.current_case+1)+' of '+data.cases.length;el('heading').textContent=c.friend+' · '+c.kind+' · '+c.case_id;
 el('previous').disabled=state.current_case===0;el('next').disabled=state.current_case===data.cases.length-1;
 el('context').textContent=c.context;el('full-context').textContent=c.full_context;el('cards').replaceChildren();
 for(const r of c.candidates){
  const card=document.createElement('article');card.className='card';
  const heading=document.createElement('h3');heading.textContent='Candidate '+r.label;card.append(heading);
  const id=document.createElement('div');id.className='id';id.textContent=r.id;card.append(id);
  const status=document.createElement('p');status.className='status';status.textContent=r.status;card.append(status);
  const text=document.createElement('pre');text.className='candidate-text';text.textContent=r.text||'(No message proposed)';card.append(text);
  if(r.memory){const details=document.createElement('details');const title=document.createElement('summary');title.textContent='Proposed memory (not saved)';const memory=document.createElement('pre');memory.textContent=r.memory;details.append(title,memory);card.append(details);}
  const label=document.createElement('label');label.textContent='Overall score for '+r.label;
  const select=document.createElement('select');select.setAttribute('aria-label','Score '+r.label);select.dataset.responseId=r.id;select.disabled=!r.attempted;
  const blank=document.createElement('option');blank.value='';blank.textContent=r.attempted?'Unscored — choose a score':'Not attempted — cannot score';select.append(blank);
  for(let score=-3;score<=3;score++){const option=document.createElement('option');option.value=String(score);option.textContent=(score>0?'+':'')+score+' — '+data.anchors[String(score)];select.append(option);}
  select.value=hasScore(r.id)?String(state.scores[r.id]):'';
  select.addEventListener('change',()=>{if(select.value==='')delete state.scores[r.id];else state.scores[r.id]=Number(select.value);save();el('progress').textContent=Object.keys(state.scores).length+' / '+ids.size+' scored';});label.append(select);card.append(label);el('cards').append(card);
 }
}
function go(index){state.current_case=index;save();render();el('heading').scrollIntoView({block:'start'});}
el('previous').onclick=()=>go(Math.max(0,state.current_case-1));el('next').onclick=()=>go(Math.min(data.cases.length-1,state.current_case+1));
el('next-unscored').onclick=()=>{for(let step=1;step<=data.cases.length;step++){const index=(state.current_case+step)%data.cases.length;if(!complete(data.cases[index])){go(index);return;}}el('notice').textContent='All responses are scored.';};
el('export').onclick=()=>{const blob=new Blob([JSON.stringify(state,null,2)+'\n'],{type:'application/json'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='scores-'+data.run_id.slice(0,12)+'.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);};
el('import').onchange=async event=>{const file=event.target.files[0];if(!file)return;try{const restored=validate(JSON.parse(await file.text()));state=restored;save();render();}catch(error){el('notice').textContent='Import rejected: '+error.message;}event.target.value='';};
if(data.summary_url){
 el('reveal').hidden=false;
 el('reveal').onclick=async()=>{
  if(!confirm('Reveal model identities and scores? This ends blinding for the responses you have scored.'))return;
  try{
   const response=await fetch(data.summary_url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(state)});
   if(!response.ok)throw Error('Score summary could not be calculated.');
   const summary=await response.json();const results=el('results');results.replaceChildren();results.hidden=false;
   const heading=document.createElement('h2');heading.textContent='Scored results — '+summary.complete_cases+' fully scored cases';results.append(heading);
   const note=document.createElement('p');note.textContent=summary.note;results.append(note);
   for(const [model,values] of Object.entries(summary.models)){const p=document.createElement('p');const mean=values.mean===null?'not scored':values.mean.toFixed(2);const matched=values.complete_case_mean===null?'no complete cases':values.complete_case_mean.toFixed(2);p.textContent=model+' — scored: '+values.scored+', neutral: '+values.neutral+', unscored: '+values.unscored+', mean: '+mean+', matched-case mean: '+matched;results.append(p);}
   results.scrollIntoView({block:'start'});
  }catch(error){el('notice').textContent=error.message;}
 };
}
render();
</script></body></html>'''
