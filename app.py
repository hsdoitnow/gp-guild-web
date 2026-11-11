import os, json, uuid, time, hmac, hashlib, base64, secrets, logging, io
from datetime import datetime, timezone, date, time as dt_time, timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime, ForeignKey, Text, select, func, UniqueConstraint, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from engine import ROLES, TIERS, DEFAULT_SCORES, generate
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

DATABASE_URL=os.getenv('DATABASE_URL','sqlite:///./gp_local.db')
if DATABASE_URL.startswith('postgres://'): DATABASE_URL=DATABASE_URL.replace('postgres://','postgresql+psycopg://',1)
if DATABASE_URL.startswith('postgresql://'): DATABASE_URL=DATABASE_URL.replace('postgresql://','postgresql+psycopg://',1)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger=logging.getLogger('gp_guild.startup')
IS_SQLITE=DATABASE_URL.startswith('sqlite:')
connect_args={'check_same_thread':False} if IS_SQLITE else {'connect_timeout':8}  # Neon pooler does not accept startup options
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
    score_a=Column(Float)
    score_b=Column(Float)
class MatchPlayer(Base):
    __tablename__='match_players'
    id=Column(Integer,primary_key=True,autoincrement=True)
    match_id=Column(String(36),ForeignKey('matches.id',ondelete='CASCADE'),nullable=False,index=True)
    name=Column(String(100),nullable=False,index=True)
    role=Column(String(30),nullable=False)
    tier=Column(String(10),nullable=False)
    points=Column(Float,nullable=False)
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
    # Upgrade historical integer score columns in-place; preserve all match rows.
    if not IS_SQLITE:
        with engine.begin() as conn:
            for table, col in [('matches','score_a'),('matches','score_b'),('match_players','points')]:
                conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN {col} TYPE DOUBLE PRECISION USING {col}::double precision'))
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
    players:list[str]
    mode:str='team'
    use_diff:bool=True
    rotate_teams:bool=True
    diff_limit:float=Field(default=1,ge=0,le=20)
    use_recent:bool=True
    recent_n:int=Field(default=1,ge=0,le=5)
class RecordIn(BaseModel):
    generation_id:str
    winner:str
class SeasonIn(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    start_date:date
    end_date:date
class ScoresIn(BaseModel):
    scores:dict[str,dict[str,float]]

def validate_roles(roles):
    if any(r not in ROLES or t not in TIERS for r,t in roles.items()): raise HTTPException(422,'캐릭터/티어 값이 올바르지 않습니다.')
def scores_from_db(s):
    setting=s.get(Setting,'scores')
    if not setting: return DEFAULT_SCORES
    try:
        raw=json.loads(setting.value)
        return {r:{t:float(raw.get(r,{}).get(t,DEFAULT_SCORES[r][t])) for t in TIERS} for r in ROLES}
    except (ValueError,TypeError,KeyError,AttributeError): return DEFAULT_SCORES
@app.get('/')
def index():
    return FileResponse(
        Path(__file__).parent / 'static' / 'index.html',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        },
    )
@app.get('/health')
def health(): return {'status':'ok'}
@app.get('/api/config')
def config(s:Session=Depends(db)):
    return {'roles':ROLES,'tiers':TIERS,'scores':scores_from_db(s)}
@app.put('/api/config/scores')
def save_scores(body:ScoresIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    try: clean={r:{t:float(body.scores[r][t]) for t in TIERS} for r in ROLES}
    except (KeyError,TypeError,ValueError): raise HTTPException(422,'모든 캐릭터/티어 점수가 필요합니다.')
    if any(not 0<=v<=99 for row in clean.values() for v in row.values()): raise HTTPException(422,'점수는 0~99만 허용합니다.')
    setting=s.get(Setting,'scores')
    if setting: setting.value=json.dumps(clean,ensure_ascii=False)
    else: s.add(Setting(key='scores',value=json.dumps(clean,ensure_ascii=False)))
    s.commit(); return {'ok':True}
@app.get('/api/profiles')
def profiles(s:Session=Depends(db)):
    return [{'name':p.name, 'available_roles':[r for r in ROLES if r in json.loads(p.roles_json or '{}')]} for p in s.scalars(select(Profile).order_by(Profile.name)).all()]
# Administrator-only bulk profile editor. Missing cells mean an unavailable role.
# Names are immutable row identifiers; Excel imports never create/delete profiles.
class BulkProfilesIn(BaseModel):
    profiles:list[ProfileIn]
    expected:dict[str,dict[str,str]]

def profile_snapshot(s):
    return {p.name:json.loads(p.roles_json) for p in s.scalars(select(Profile).order_by(Profile.name)).all()}

def check_bulk(current, proposed, expected):
    if len(proposed)>2000 or len(proposed)!=len(set(proposed)):
        raise HTTPException(422,'프로필 중복 또는 최대 인원(2000명) 초과')
    if set(proposed)!=set(current) or set(expected)!=set(current):
        raise HTTPException(409,'프로필 명단이 변경되었습니다. 최신 목록을 다시 다운로드/조회하세요.')
    for name, roles in proposed.items():
        if not name.strip() or len(name)>100:raise HTTPException(422,'닉네임 오류')
        validate_roles(roles)
    if expected!=current:
        raise HTTPException(409,'다른 관리자가 프로필을 변경했습니다. 새로고침 후 다시 시도하세요.')
    return [{'name':name,'role':role,'before':current[name].get(role),'after':proposed[name].get(role)}
            for name in sorted(current) for role in ROLES if current[name].get(role)!=proposed[name].get(role)]

@app.get('/api/admin/profiles-overview')
def admin_profiles_overview(request:Request,s:Session=Depends(db)):
    require_admin(request)
    return [{'name':name,'roles':roles} for name,roles in profile_snapshot(s).items()]

@app.put('/api/admin/profiles-overview')
def admin_profiles_save(body:BulkProfilesIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    current=profile_snapshot(s)
    proposed={p.name:p.roles for p in body.profiles}
    changes=check_bulk(current,proposed,body.expected)
    for name in {x['name'] for x in changes}:
        s.get(Profile,name).roles_json=json.dumps(proposed[name],ensure_ascii=False)
    s.commit()
    return {'ok':True,'changed':len(changes)}

@app.get('/api/admin/profiles-excel')
def admin_profiles_excel(request:Request,s:Session=Depends(db)):
    require_admin(request)
    current=profile_snapshot(s)
    wb=Workbook();ws=wb.active;ws.title='프로필'
    ws.append(['닉네임',*ROLES]);ws.freeze_panes='B2';ws.auto_filter.ref=f'A1:E{len(current)+1}'
    for name, rolemap in current.items():ws.append([name,*[rolemap.get(role,'') for role in ROLES]])
    for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='244C83')
    ws.column_dimensions['A'].width=24
    for col in 'BCDE':ws.column_dimensions[col].width=19
    from openpyxl.worksheet.datavalidation import DataValidation
    dv=DataValidation(type='list',formula1='"'+','.join(TIERS)+'"',allow_blank=True)
    dv.error='허용된 티어를 선택하세요.';dv.showErrorMessage=True;ws.add_data_validation(dv);dv.add('B2:E2001')
    ws2=wb.create_sheet('_snapshot');ws2.sheet_state='veryHidden'
    ws2.append(['닉네임',*ROLES])
    for name, rolemap in current.items():ws2.append([name,*[rolemap.get(role,'') for role in ROLES]])
    out=io.BytesIO();wb.save(out);out.seek(0)
    return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="gp_profiles.xlsx"','Cache-Control':'no-store'})

def parse_excel(data):
    try:wb=load_workbook(io.BytesIO(data),read_only=True,data_only=True)
    except Exception:raise HTTPException(422,'올바른 XLSX 파일이 아닙니다.')
    try:
        if '프로필' not in wb.sheetnames or '_snapshot' not in wb.sheetnames:raise HTTPException(422,'홈페이지에서 다운로드한 원본 XLSX를 사용하세요.')
        def parse_sheet(ws):
            rows=ws.iter_rows(values_only=True)
            header=next(rows,None)
            if not header or tuple(header[:5])!=('닉네임',*ROLES):raise HTTPException(422,'엑셀 열 제목이 변경되었습니다.')
            output={}
            for idx,row in enumerate(rows,start=2):
                if idx>2001:raise HTTPException(422,'최대 2000명까지 허용합니다.')
                if not row or all(v is None for v in row):continue
                name=row[0]
                if not isinstance(name,str) or not name.strip() or name!=name.strip() or len(name)>100 or name in output:
                    raise HTTPException(422,f'{idx}행: 닉네임 오류 또는 중복')
                rolemap={}
                for i,role in enumerate(ROLES,start=1):
                    value=row[i] if i<len(row) else None
                    if value is None or value=='':continue
                    if not isinstance(value,str) or value not in TIERS:raise HTTPException(422,f'{idx}행 {role}: 잘못된 티어')
                    rolemap[role]=value
                output[name]=rolemap
            return output
        return parse_sheet(wb['프로필']),parse_sheet(wb['_snapshot'])
    finally:wb.close()

@app.post('/api/admin/profiles-excel/preview')
async def admin_profiles_excel_preview(request:Request,file:UploadFile=File(...),s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    if not file.filename or not file.filename.lower().endswith('.xlsx'):raise HTTPException(422,'XLSX 파일만 업로드하세요.')
    data=await file.read(2_000_001)
    if len(data)>2_000_000:raise HTTPException(413,'엑셀 파일은 2MB 이하만 허용합니다.')
    proposed,expected=parse_excel(data)
    changes=check_bulk(profile_snapshot(s),proposed,expected)
    return {'changes':changes,'profiles':[{'name':n,'roles':r} for n,r in proposed.items()],'expected':expected}

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
    names=[name.strip() for name in body.players]
    if len(names)!=8 or any(not n or len(n)>100 for n in names) or len(set(names))!=8: raise HTTPException(422,'중복 없는 8명의 닉네임을 입력하세요.')
    saved=profile_snapshot(s)
    if any(n not in saved for n in names): raise HTTPException(422,'등록되지 않은 프로필이 있습니다. 관리자에게 등록을 요청하세요.')
    players=[{'name':n,'roles':saved[n]} for n in names]
    # Only finalized games affect recent-character exclusion.
    rows=s.execute(select(MatchPlayer.name,MatchPlayer.role,Match.created_at,Match.id).join(Match,Match.id==MatchPlayer.match_id).where(MatchPlayer.name.in_(names)).order_by(Match.created_at.desc(),Match.id.desc()).limit(400)).all()
    games={}
    for name,role,created,mid in rows: games.setdefault(mid,{})[name]=role
    history=list(reversed(list(games.values())))
    # Fetch each player’s own last 10 games (not the last 10 guild games).
    role_history={name:[] for name in names}
    recent_exclusion_roles={}
    cutoff=datetime.now(timezone.utc)-timedelta(hours=12)
    for name in role_history:
        records=s.execute(
            select(MatchPlayer.role,Match.created_at).join(Match,Match.id==MatchPlayer.match_id)
            .where(MatchPlayer.name==name)
            .order_by(Match.created_at.desc(),Match.id.desc()).limit(10)
        ).all()
        role_history[name]=[role for role,_ in records]
        # Only the player's last match within the rolling 12-hour window
        # triggers the hard exclusion; older matches still count for fairness.
        recent_exclusion_roles[name]=[]
        if records:
            last_role,last_played=records[0]
            if last_played is not None:
                if last_played.tzinfo is None:
                    last_played=last_played.replace(tzinfo=timezone.utc)
                if cutoff<=last_played<=datetime.now(timezone.utc):
                    recent_exclusion_roles[name]=[last_role]
    previous_teams=None
    if body.mode=='team' and body.rotate_teams:
        # Latest finalized match for exactly the same 8 players.
        latest=s.scalar(select(Match).order_by(Match.created_at.desc(),Match.id.desc()).limit(1))
        if latest:
            members=s.scalars(select(MatchPlayer).where(MatchPlayer.match_id==latest.id)).all()
            if len(members)==8 and {p.name for p in members}==set(names):
                previous_teams={t:{p.name for p in members if p.team==t} for t in ('A','B')}
    try:
        result=generate(players,body.mode,scores_from_db(s),body.diff_limit,body.use_diff,body.recent_n if body.use_recent else 0,history,previous_teams,role_history=role_history,recent_exclusion_roles=recent_exclusion_roles)
        # Never expose tiers, player scores or team totals to a public client.
        if result.get('mode')=='team':
            generation_id=str(uuid.uuid4())
            pending={'expires':time.time()+1800,'participants':result['participants'],'scores':result.get('scores',{})}
            s.add(Setting(key='pending:'+generation_id,value=json.dumps(pending,ensure_ascii=False)))
            s.commit()
            result['generation_id']=generation_id
        return {'mode':result.get('mode'),'participants':[{'name':p['name'],'role':p['role'],'team':p.get('team')} for p in result['participants']], 'generation_id':result.get('generation_id'), 'scores':result.get('scores') if result.get('mode')=='team' else None, 'diff':result.get('diff') if result.get('mode')=='team' else None}
    except ValueError as e: raise HTTPException(422,str(e))
class ManualMatchIn(BaseModel):
    played_at:datetime
    winner:str
    teams:dict[str,dict[str,str]]

@app.post('/api/matches/manual')
def record_manual(body:ManualMatchIn,request:Request,s:Session=Depends(db)):
    same_origin(request);require_admin(request)
    if body.winner not in ('A','B') or set(body.teams)!= {'A','B'}:
        raise HTTPException(422,'A/B팀과 승리팀을 확인하세요.')
    played=body.played_at
    if played.tzinfo is None:
        raise HTTPException(422,'경기 날짜에 시간대가 필요합니다.')
    played=played.astimezone(timezone.utc)
    if played>datetime.now(timezone.utc)+timedelta(minutes=5):
        raise HTTPException(422,'미래 경기는 등록할 수 없습니다.')
    if played.year<2000:
        raise HTTPException(422,'경기 날짜가 올바르지 않습니다.')
    all_names=[]
    for team in ('A','B'):
        if set(body.teams[team])!=set(ROLES):
            raise HTTPException(422,'각 팀의 캐릭터를 모두 지정하세요.')
        all_names.extend(body.teams[team].values())
    all_names=[n.strip() for n in all_names]
    if any(not n or len(n)>100 for n in all_names) or len(set(all_names))!=8:
        raise HTTPException(422,'닉네임 8명이 중복 없이 필요합니다.')
    saved={p.name:json.loads(p.roles_json) for p in s.scalars(select(Profile).where(Profile.name.in_(all_names))).all()}
    if len(saved)!=8: raise HTTPException(422,'등록된 프로필 8명을 선택하세요.')
    score_table=scores_from_db(s)
    players=[]
    totals={'A':0,'B':0}
    for team in ('A','B'):
        for role in ROLES:
            name=body.teams[team][role].strip()
            tier=saved[name].get(role)
            if tier not in TIERS:
                raise HTTPException(422,f'{name}님은 {role} 가능 캐릭터로 등록되어 있지 않습니다.')
            points=score_table[role][tier]
            totals[team]+=points
            players.append((team,role,name,tier,points))
    mid=str(uuid.uuid4())
    s.add(Match(id=mid,created_at=played,winner=body.winner,score_a=totals['A'],score_b=totals['B']))
    for team,role,name,tier,points in players:
        s.add(MatchPlayer(match_id=mid,name=name,role=role,tier=tier,points=points,team=team))
    s.commit()
    logger.info('Manual match recorded: id=%s, played_at=%s',mid,played.isoformat())
    return {'ok':True,'id':mid}

@app.post('/api/matches')
def record(body:RecordIn,request:Request,s:Session=Depends(db)):
    same_origin(request)
    if body.winner not in ('A','B'): raise HTTPException(422,'승리팀은 A 또는 B입니다.')
    try: uuid.UUID(body.generation_id)
    except (ValueError,TypeError): raise HTTPException(422,'팀 생성 ID가 올바르지 않습니다.')
    key='pending:'+body.generation_id
    # Lock pending row to prevent double submissions on PostgreSQL.
    pending_row=s.execute(select(Setting).where(Setting.key==key).with_for_update()).scalar_one_or_none()
    if not pending_row: raise HTTPException(409,'팀 생성 결과가 만료되었거나 이미 기록되었습니다.')
    payload=json.loads(pending_row.value)
    if payload['expires']<time.time():
        s.delete(pending_row);s.commit()
        raise HTTPException(409,'팀 생성 후 30분이 지났습니다. 다시 생성하세요.')
    participants=payload['participants']
    mid=str(uuid.uuid4())
    scores=payload['scores']
    s.add(Match(id=mid,winner=body.winner,score_a=scores.get('A'),score_b=scores.get('B')))
    for p in participants:
        s.add(MatchPlayer(match_id=mid,name=p['name'],role=p['role'],tier=p['tier'],points=p['points'],team=p['team']))
    s.delete(pending_row)
    s.commit()
    return {'ok':True,'id':mid}
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
    for p in parts: grouped.setdefault(p.match_id,[]).append({'name':p.name,'role':p.role,'team':p.team})
    return [{'id':m.id,'created_at':m.created_at.isoformat(),'winner':m.winner,'score_a':m.score_a,'score_b':m.score_b,'participants':grouped.get(m.id,[])} for m in ms]
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
        p['total']=p['wins']+p['losses'];p['win_rate']=round(100*p['wins']/p['total'],1);p['score']=p['wins']*10+p['losses']*2
        for r in p['by_role'].values():
            r['total']=r['wins']+r['losses'];r['win_rate']=round(100*r['wins']/r['total'],1);r['score']=r['wins']*10+r['losses']*2
        result.append(p)
    return sorted(result,key=lambda p:(-p['score'],-p['wins'],-p['win_rate'],p['name']))


@app.get('/api/player-analysis')
def player_analysis(name:str=Query(...,min_length=1,max_length=100),season_id:int|None=None,s:Session=Depends(db)):
    """Personal-only match analytics; no other player names or private tier data."""
    from collections import defaultdict
    name=name.strip()
    if not name:raise HTTPException(422,'닉네임을 입력하세요.')
    season=season_bounds(s,season_id)
    q=select(Match, MatchPlayer.role, MatchPlayer.team).join(MatchPlayer,MatchPlayer.match_id==Match.id).where(MatchPlayer.name==name)
    if season:q=q.where(Match.created_at>=season.start_date,Match.created_at<season.end_date)
    rows=s.execute(q.order_by(Match.created_at.asc(),Match.id.asc())).all()
    by_role=defaultdict(lambda:[0,0]);monthly=defaultdict(lambda:[0,0]);monthly_by_role={r:defaultdict(lambda:[0,0]) for r in ROLES};recent=[];wins=0
    best_streak=worst_streak=current=0;running_score=0;score_trend=[]
    cumulative_by_role=defaultdict(lambda:[0,0]); win_rate_trend={"전체":[]}; win_rate_trend.update({r:[] for r in ROLES})
    winning_roles=set()
    for match,role,team in rows:
        won=(team==match.winner)
        wins+=int(won)
        by_role[role][0]+=int(won);by_role[role][1]+=1
        if won:winning_roles.add(role)
        month=as_utc(match.created_at).astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m')
        monthly[month][0]+=int(won);monthly[month][1]+=1
        if role in monthly_by_role:
            monthly_by_role[role][month][0]+=int(won);monthly_by_role[role][month][1]+=1
        current=max(0,current)+1 if won else min(0,current)-1
        best_streak=max(best_streak,current);worst_streak=max(worst_streak,-current)
        running_score+=10 if won else 2
        score_trend.append({'game':len(score_trend)+1,'score':running_score})
        game_number=len(score_trend)
        win_rate_trend['전체'].append({'game':game_number,'date':as_utc(match.created_at).isoformat(),
            'total':game_number,'wins':wins,'losses':game_number-wins,'win_rate':round(100*wins/game_number,2)})
        role_stat=cumulative_by_role[role]
        role_stat[0]+=int(won);role_stat[1]+=1
        win_rate_trend[role].append({'game':role_stat[1],'date':as_utc(match.created_at).isoformat(),
            'total':role_stat[1],'wins':role_stat[0],'losses':role_stat[1]-role_stat[0],
            'win_rate':round(100*role_stat[0]/role_stat[1],2)})
        recent.append({'date':as_utc(match.created_at).isoformat(),'won':won,'role':role})
    total=len(rows)
    recent10=recent[-10:]
    recent_wins=sum(x['won'] for x in recent10)
    previous10=recent[-20:-10]
    previous_wins=sum(x['won'] for x in previous10)
    qualified=[(r,v) for r,v in by_role.items() if v[1]>=1]
    qualified.sort(key=lambda x:(-x[1][0]/x[1][1],-x[1][1],x[0]))
    best_role={'role':qualified[0][0],'total':qualified[0][1][1],
               'win_rate':round(100*qualified[0][1][0]/qualified[0][1][1],1)} if qualified else None
    monthly_data=[{'month':k,'wins':v[0],'losses':v[1]-v[0],'total':v[1],
                   'win_rate':round(100*v[0]/v[1],1)} for k,v in sorted(monthly.items())]
    monthly_role_data={'전체':monthly_data}
    for role in ROLES:
        monthly_role_data[role]=[{'month':k,'wins':v[0],'losses':v[1]-v[0],'total':v[1],
            'win_rate':round(100*v[0]/v[1],1)} for k,v in sorted(monthly_by_role[role].items())]
    busiest=max(monthly_data,key=lambda x:(x['total'],-int(x['month'].replace('-',''))),default=None)
    achievements=[{'title':'첫 승리','achieved':wins>=1,'progress':min(wins,1),'target':1},
                  {'title':'5연승','achieved':best_streak>=5,'progress':min(best_streak,5),'target':5},
                  {'title':'10승','achieved':wins>=10,'progress':min(wins,10),'target':10},
                  {'title':'50승','achieved':wins>=50,'progress':min(wins,50),'target':50},
                  {'title':'100승','achieved':wins>=100,'progress':min(wins,100),'target':100},
                  {'title':'100경기','achieved':total>=100,'progress':min(total,100),'target':100},
                  {'title':'4캐릭터 승리','achieved':all(r in winning_roles for r in ROLES),
                   'progress':sum(r in winning_roles for r in ROLES),'target':4}]
    return {'name':name,'total':total,'wins':wins,'losses':total-wins,
            'win_rate':round(100*wins/total,1) if total else 0,'score':running_score,
            'by_role':{r:{'wins':v[0],'losses':v[1]-v[0],'total':v[1],
            'win_rate':round(100*v[0]/v[1],1),'score':v[0]*10+(v[1]-v[0])*2} for r,v in by_role.items()},
            'best_streak':best_streak,'worst_streak':worst_streak,'current_streak':current,
            'recent':list(reversed(recent10)),'recent_wins':recent_wins,
            'recent_win_rate':round(100*recent_wins/len(recent10),1) if recent10 else 0,
            'previous_10_total':len(previous10),'previous_10_wins':previous_wins,
            'previous_10_win_rate':round(100*previous_wins/len(previous10),1) if previous10 else None,
            'best_role':best_role,'monthly':monthly_data,'monthly_by_role':monthly_role_data,'busiest_month':busiest,
            'score_trend':score_trend,'win_rate_trend':win_rate_trend,'achievements':achievements}
