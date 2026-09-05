import os, requests
from energy_catalog import EnergyDrink
SEARX="https://searx.be/search"
OR="https://openrouter.ai/api/v1/chat/completions"
def ai_search_energy(q:str):
 k=os.getenv("OPENROUTER_API_KEY")
 try:
  r=requests.get(SEARX,params={"q":q+" energy drink caffeine taurine volume","format":"json"},timeout=12).json()
  txt="\n".join([f"{x.get('title')}\n{x.get('content')}" for x in r.get('results',[])[:5]])
  if not k or not txt:return None
  p=f"Extract JSON: name,brand,quantity,caffeine_mg,taurine_mg from: {txt}"
  j=requests.post(OR,headers={"Authorization":f"Bearer {k}"},json={"model":"google/gemma-3-27b-it:free","messages":[{"role":"user","content":p}]},timeout=25).json()
  import json,re
  m=re.search(r"\{.*\}",j["choices"][0]["message"]["content"],re.S)
  d=json.loads(m.group())
  return EnergyDrink(code="ai-"+q,name=d["name"],brand=d.get("brand","?"),quantity=d.get("quantity","?"),caffeine_mg=float(d["caffeine_mg"]),taurine_mg=float(d["taurine_mg"]),source_url=SEARX)
 except:return None
