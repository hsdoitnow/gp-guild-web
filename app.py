import os, json, uuid, time, hmac, hashlib, base64, secrets, logging
from datetime import datetime, timezone, date, time as dt_time, timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text, select, func, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from engine import ROLES, TIERS, DEFAULT_SCORES, generate

DATABASE_URL=os.getenv('DATABASE_URL','sqlite:///./gp_local.db')
if DATABASE_URL.startswith('postgres://'): DATABASE_URL=DATABASE_URL.replace('postgres://','postgresql+psycopg://',1)
if DATABASE_URL.startswith('postgresql://'): DATABASE_URL=DATABASE_URL.replace('postgresql://','postgresql+psycopg://',1)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger=logging.getLogger('gp_guild.startup')
IS_SQLITE=DATABASE_URL.startswith('sqlite:')
connect_args={'check_same_thread':False} if IS_SQLITE else {'connect_timeout':8, 'options':'-c statement_timeout=15000 -c lock_timeout=8000'}
logger.info('Configuring database engine (backend=%s)', 'sqlite' if IS_SQLITE else 'postgresql')
engine=create_engine(DATABASE_URL, pool_pre_ping=True, pool_timeout=10, connect_args=connect_args)
SessionLocal=sessionmaker(bind=engine)
Base=declarative_base()
class Profile(Base):
    __tablename__='profiles'
    name=Column(String(100),primary_key=True)
    roles_json=Column(Text,nullable=False,default='{}')
class Setting(Base):
    __tablename__='settings'
    key=Column(String(100),primary_key=True)
    value=Column(Text,nullable=False)
class Match(Base):
    __tablename__='matches'
    id=Column(String(36),primary_key=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)
    winner=Column(String(1),nullable=False)
    score_a=Column(Integer)
    score_b=Column(Integer)
class MatchPlayer(Base):
    __tablename__='match_players'
    id=Column(Integer,primary_key=True,autoincrement=True)
    match_id=Column(String(36),ForeignKey('matches.id',ondelete='CASCADE'),nullable=False,index=True)
    name=Column(String(100),nullable=False,index=True)
    role=Column(String(30),nullable=False)
    tier=Column(String(10),nullable=False)
    points=Column(Integer,nullable=False)
    team=Column(String(1),nullable=False)
    __table_args__=(UniqueConstraint('match_id','name'),)
class Season(Base):
    __tablename__='seasons'
    id=Column(Integer,primary_key=True,autoincrement=True)
    name=Column(String(100),nullable=False,unique=True)
    start_date=Column(DateTime(timezone=True),nullable=False)
    end_date=Column(DateTime(timezone=True),nullable=False)  # exclusive UTC bound
logger.info('Database schema initialization starting')
try:
    Base.metadata.create_all(engine)
except Exception:
    logger.exception('Database schema initialization failed. Check Neon availability, DATABASE_URL, and DB permissions.')
    raise
logger.info('Database schema initialization completed')
app=FastAPI(title='GP길드 PK 팀 생성기')
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
def db():
    with SessionLocal() as s: yield s

# Signed, expiring, HttpOnly admin cookie. Password is never sent to the client after login.
ADMIN_PASSWORD=os.getenv('ADMIN_PASSWORD','')
SESSION_KEY=hashlib.sha256((os.getenv('ADMIN_SESSION_SECRET') or ('gp-admin:'+ADMIN_PASSWORD)).encode()).digest()
COOKIE_NAME='gp_admin_session'
SESSION_SECONDS=8*60*60
LOGIN_ATTEMPTS={}
def token_for_admin():
    expiry=str(int(time.time())+SESSION_SECONDS)
    sig=hmac.new(SESSION_KEY,expiry.encode(),hashlib.sha256).hexdigest()
    return expiry+'.'+sig
def is_admin(request:Request):
    if not ADMIN_PASSWORD:return False
    raw=request.cookies.get(COOKIE_NAME,'')
    try:
        expiry,sig=raw.split('.',1)
        if int(expiry)<time.time():return False
        expected=hmac.new(SESSION_KEY,expiry.encode(),hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig,expected)
    except (ValueError,TypeError):return False
def require_admin(request:Request):
    if not is_admin(request):raise HTTPException(403,'관리자 로그인이 필요합니다.')
def same_origin(request:Request):
    origin=request.headers.get('origin')
    if origin:
        from urllib.parse import urlsplit
        origin_host=urlsplit(origin).netloc.lower()
        target_host=request.headers.get('host','').lower()
        if origin_host!=target_host:raise HTTPException(403,'허용되지 않은 요청 출처입니다.')
class AdminLoginIn(BaseModel):
    password:str
@app.get('/api/admin/status')
def admin_status(request:Request):
    return {'is_admin':is_admin(request),'configured':bool(ADMIN_PASSWORD)}
@app.post('/api/admin/login')
def admin_login(body:AdminLoginIn,request:Request,response:Response):
    same_origin(request)
    if not ADMIN_PASSWORD:raise HTTPException(503,'Render 환경변수 ADMIN_PASSWORD를 설정하세요.')
    ip=request.client.host if request.client else 'unknown'
    now=time.time()
    attempts=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if now-t<900]
    if len(attempts)>=10:raise HTTPException(429,'로그인 시도가 많습니다. 15분 뒤 다시 시도하세요.')
    if not hmac.compare_digest(body.password,ADMIN_PASSWORD):
        attempts.append(now);LOGIN_ATTEMPTS[ip]=attempts
        raise HTTPException(401,'비밀번호가 올바르지 않습니다.')
    LOGIN_ATTEMPTS.pop(ip,None)
    secure=request.url.scheme=='https' or request.headers.get('x-forwarded-proto')=='https'
    response.set_cookie(COOKIE_NAME,token_for_admin(),max_age=SESSION_SECONDS,httponly=True,secure=secure,samesite='lax',path='/')
    return {'ok':True}
@app.post('/api/admin/logout')
def admin_logout(request:Request,response:Response):
    same_origin(request)
    response.delete_cookie(COOKIE_NAME,path='/')
    return {'ok':True}
class ProfileIn(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    roles:dict[str,str]
class GenerateIn(BaseModel):
    players:list[ProfileIn]
    mode:str='team'
    use_diff:bool=True
    diff_limit:int=Field(default=1,ge=0,le=20)
    use_recent:bool=True
    recent_n:int=Field(default=1,ge=0,le=5)
class RecordIn(BaseModel):
    participants:list[dict]
    winner:str
    scores:dict|None=None
class SeasonIn(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    start_date:date
    end_date:date
class ScoresIn(BaseModel):
    scores:dict[str,dict[str,int]]

def validate_roles(roles):
    if any(r not in ROLES or t not in TIERS for r,t in roles.items()): raise HTTPException(422,'캐릭터/티어 값이 올바르지 않습니다.')
def scores_from_db(s):
    setting=s.get(Setting,'scores')
    if not setting: return DEFAULT_SCORES
    try:
        raw=json.loads(setting.value)
        return {r:{t:int(raw[r][t]) for t in TIERS} for r in ROLES}
    except (ValueError,TypeError,KeyError): return DEFAULT_SCORES
@app.get('/')
def index(): return FileResponse(Path(__file__).parent/'static'/'index.html')
@app.get('/health')
def health(): return {'status':'ok'}
@app.get('/api/config')
def config(s:Session=Depends(db)):
    return {'roles':ROLES,'tiers':TIERS,'scores':scores_from_db(s)}
@app.put('/api/config/scores')
def save_scores(body:ScoresIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    try: clean={r:{t:int(body.scores[r][t]) for t in TIERS} for r in ROLES}
    except (KeyError,TypeError,ValueError): raise HTTPException(422,'모든 캐릭터/티어 점수가 필요합니다.')
    if any(not 0<=v<=99 for row in clean.values() for v in row.values()): raise HTTPException(422,'점수는 0~99만 허용합니다.')
    setting=s.get(Setting,'scores')
    if setting: setting.value=json.dumps(clean,ensure_ascii=False)
    else: s.add(Setting(key='scores',value=json.dumps(clean,ensure_ascii=False)))
    s.commit(); return {'ok':True}
@app.get('/api/profiles')
def profiles(s:Session=Depends(db)):
    return [{'name':p.name,'roles':json.loads(p.roles_json)} for p in s.scalars(select(Profile).order_by(Profile.name)).all()]
@app.put('/api/profiles')
def upsert_profile(body:ProfileIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    name=body.name.strip()
    if not name: raise HTTPException(422,'이름을 입력하세요.')
    validate_roles(body.roles)
    p=s.get(Profile,name)
    if p: p.roles_json=json.dumps(body.roles,ensure_ascii=False)
    else: s.add(Profile(name=name,roles_json=json.dumps(body.roles,ensure_ascii=False)))
    s.commit(); return {'ok':True}
@app.delete('/api/profiles/{name}')
def delete_profile(name:str,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    p=s.get(Profile,name)
    if not p: raise HTTPException(404,'프로필이 없습니다.')
    s.delete(p);s.commit();return {'ok':True}
@app.post('/api/profiles/import')
def import_profiles(data:dict[str,dict],request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    if len(data)>2000: raise HTTPException(422,'한 번에 2000명 이하만 가져올 수 있습니다.')
    for name,payload in data.items():
        if not isinstance(name,str) or not name.strip() or len(name)>100 or not isinstance(payload,dict): raise HTTPException(422,'프로필 형식 오류')
        roles=payload.get('roles',{})
        if not isinstance(roles,dict): raise HTTPException(422,'roles 형식 오류')
        validate_roles(roles)
        p=s.get(Profile,name.strip())
        if p: p.roles_json=json.dumps(roles,ensure_ascii=False)
        else: s.add(Profile(name=name.strip(),roles_json=json.dumps(roles,ensure_ascii=False)))
    s.commit();return {'ok':True,'count':len(data)}
@app.post('/api/generate')
def generate_api(body:GenerateIn,s:Session=Depends(db)):
    if len(body.players)!=8: raise HTTPException(422,'8명을 입력하세요.')
    for p in body.players: validate_roles(p.roles)
    # Only finalized games affect recent-character exclusion.
    rows=s.execute(select(MatchPlayer.name,MatchPlayer.role,Match.created_at,Match.id).join(Match,Match.id==MatchPlayer.match_id).where(MatchPlayer.name.in_([p.name.strip() for p in body.players])).order_by(Match.created_at.desc(),Match.id.desc()).limit(400)).all()
    games={}
    for name,role,created,mid in rows: games.setdefault(mid,{})[name]=role
    history=list(reversed(list(games.values())))
    try:
        return generate([p.model_dump() for p in body.players],body.mode,scores_from_db(s),body.diff_limit,body.use_diff,body.recent_n if body.use_recent else 0,history)
    except ValueError as e: raise HTTPException(422,str(e))
@app.post('/api/matches')
def record(body:RecordIn,s:Session=Depends(db)):
    if body.winner not in ('A','B'): raise HTTPException(422,'승리팀은 A 또는 B입니다.')
    if len(body.participants)!=8: raise HTTPException(422,'8명의 참가자가 필요합니다.')
    names=set(); team_roles={'A':set(),'B':set()}; teams={'A':0,'B':0}
    for p in body.participants:
        name=p.get('name');role=p.get('role');tier=p.get('tier');team=p.get('team');points=p.get('points')
        if not isinstance(name,str) or not name.strip() or len(name)>100 or name in names or role not in ROLES or tier not in TIERS or team not in teams or type(points)!=int or not 0<=points<=99: raise HTTPException(422,'참가자 정보가 올바르지 않습니다.')
        if role in team_roles[team]: raise HTTPException(422,'팀별 캐릭터가 중복되었습니다.')
        names.add(name);team_roles[team].add(role);teams[team]+=1
    if teams!={'A':4,'B':4} or any(team_roles[t]!=set(ROLES) for t in teams): raise HTTPException(422,'각 팀에 캐릭터별 1명씩 있어야 합니다.')
    mid=str(uuid.uuid4());scores=body.scores or {}
    s.add(Match(id=mid,winner=body.winner,score_a=scores.get('A'),score_b=scores.get('B')))
    for p in body.participants: s.add(MatchPlayer(match_id=mid,name=p['name'].strip(),role=p['role'],tier=p['tier'],points=p['points'],team=p['team']))
    s.commit();return {'ok':True,'id':mid}
class MatchEditIn(BaseModel):
    winner:str
@app.patch('/api/matches/{match_id}')
def edit_match(match_id:str,body:MatchEditIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    if body.winner not in ('A','B'):raise HTTPException(422,'승리팀은 A 또는 B입니다.')
    match=s.get(Match,match_id)
    if not match:raise HTTPException(404,'경기 기록을 찾을 수 없습니다.')
    match.winner=body.winner
    s.commit()
    return {'ok':True}
@app.delete('/api/matches/{match_id}')
def delete_match(match_id:str,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    match=s.get(Match,match_id)
    if not match:raise HTTPException(404,'경기 기록을 찾을 수 없습니다.')
    s.query(MatchPlayer).filter(MatchPlayer.match_id==match_id).delete(synchronize_session=False)
    s.delete(match)
    s.commit()
    return {'ok':True}
@app.get('/api/matches')
def matches(limit:int=Query(50,ge=1,le=200),s:Session=Depends(db)):
    ms=s.scalars(select(Match).order_by(Match.created_at.desc(),Match.id.desc()).limit(limit)).all()
    if not ms:return []
    parts=s.scalars(select(MatchPlayer).where(MatchPlayer.match_id.in_([m.id for m in ms]))).all()
    grouped={}
    for p in parts: grouped.setdefault(p.match_id,[]).append({'name':p.name,'role':p.role,'tier':p.tier,'points':p.points,'team':p.team})
    return [{'id':m.id,'created_at':m.created_at.isoformat(),'winner':m.winner,'scores':{'A':m.score_a,'B':m.score_b},'participants':grouped.get(m.id,[])} for m in ms]
def season_bounds(s:Session,season_id:int|None):
    if season_id is None:return None
    season=s.get(Season,season_id)
    if not season:raise HTTPException(404,'시즌을 찾을 수 없습니다.')
    return season

def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

def season_payload(season:Season):
    kst=timezone(timedelta(hours=9))
    return {'id':season.id,'name':season.name,'start_date':as_utc(season.start_date).astimezone(kst).date().isoformat(),
            'end_date':(as_utc(season.end_date).astimezone(kst)-timedelta(days=1)).date().isoformat()}

@app.get('/api/seasons')
def list_seasons(s:Session=Depends(db)):
    return [season_payload(x) for x in s.scalars(select(Season).order_by(Season.start_date.desc())).all()]

def check_season(body:SeasonIn,s:Session,exclude:int|None=None):
    if not body.name.strip() or body.start_date>body.end_date:raise HTTPException(422,'시즌명과 시작·종료일을 확인하세요.')
    start=datetime.combine(body.start_date,dt_time.min,tzinfo=timezone.utc)-timedelta(hours=9)
    end=datetime.combine(body.end_date+timedelta(days=1),dt_time.min,tzinfo=timezone.utc)-timedelta(hours=9)
    for old in s.scalars(select(Season)).all():
        if old.id==exclude:continue
        if old.name==body.name.strip():raise HTTPException(409,'동일한 시즌명이 존재합니다.')
        if start<as_utc(old.end_date) and end>as_utc(old.start_date):raise HTTPException(409,'기존 시즌과 기간이 겹칩니다.')
    return start,end

@app.post('/api/seasons')
def create_season(body:SeasonIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    start,end=check_season(body,s)
    item=Season(name=body.name.strip(),start_date=start,end_date=end)
    s.add(item);s.commit();s.refresh(item)
    return season_payload(item)

@app.put('/api/seasons/{season_id}')
def update_season(season_id:int,body:SeasonIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    item=s.get(Season,season_id)
    if not item:raise HTTPException(404,'시즌을 찾을 수 없습니다.')
    start,end=check_season(body,s,season_id)
    item.name=body.name.strip();item.start_date=start;item.end_date=end
    s.commit();return season_payload(item)

@app.delete('/api/seasons/{season_id}')
def delete_season(season_id:int,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    item=s.get(Season,season_id)
    if not item:raise HTTPException(404,'시즌을 찾을 수 없습니다.')
    s.delete(item);s.commit();return {'ok':True}

@app.get('/api/stats')
def stats(name:str|None=None,season_id:int|None=None,role:str|None=None,s:Session=Depends(db)):
    q=select(MatchPlayer.name,MatchPlayer.role,MatchPlayer.team,Match.winner).join(Match,Match.id==MatchPlayer.match_id)
    if name:q=q.where(MatchPlayer.name==name.strip())
    if role:
        if role not in ROLES:raise HTTPException(422,'캐릭터가 올바르지 않습니다.')
        q=q.where(MatchPlayer.role==role)
    season=season_bounds(s,season_id)
    if season:q=q.where(Match.created_at>=season.start_date,Match.created_at<season.end_date)
    rows=s.execute(q).all();people={}
    for n,role,team,winner in rows:
        obj=people.setdefault(n,{'name':n,'wins':0,'losses':0,'by_role':{}})
        won=int(team==winner);obj['wins']+=won;obj['losses']+=1-won
        r=obj['by_role'].setdefault(role,{'wins':0,'losses':0});r['wins']+=won;r['losses']+=1-won
    result=[]
    for p in people.values():
        p['total']=p['wins']+p['losses'];p['win_rate']=round(100*p['wins']/p['total'],1)
        for r in p['by_role'].values():
            r['total']=r['wins']+r['losses'];r['win_rate']=round(100*r['wins']/r['total'],1)
        result.append(p)
    return sorted(result,key=lambda p:(-p['win_rate'],-p['total'],p['name']))
