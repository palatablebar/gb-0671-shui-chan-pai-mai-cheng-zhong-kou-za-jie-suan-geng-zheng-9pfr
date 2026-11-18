from fastapi import Request
from fastapi.templating import Jinja2Templates
from app import config
from app.config import ROOT, META
from app.engine import evaluate
from app import store
from app.modules.registry import service, store as rstore

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def render(request:Request,key):
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(config.DB_PATH,run_id)
    else:
        state=store.save(config.DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    return templates.TemplateResponse(request=request,name=f'{key}.html',context={'meta':META,'key':key,'state':state,'view':state['views'][key]})

def render_registry(request:Request):
    from app.modules.registry.reasons import STATE_INGESTED
    entries=rstore.list_entries(config.REGISTRY_DB_PATH)
    now=service.now_iso()
    open_count=sum(1 for e in entries if e['state']!=STATE_INGESTED and e['open_at'] and e['open_at']<=now)
    return templates.TemplateResponse(request=request,name='registry.html',context={
        'meta':META,'key':None,'state':None,'view':None,
        'entries':entries,'now':now,'open_count':open_count,
    })
