"""Original PyQt team's role assignment and score-balance logic, extracted for the web."""
import random
ROLES = ['드루이드','어쌔','네크','슴딘']
TIERS = ['SSS','SS','S','A']
DEFAULT_SCORES = {r:dict(zip(TIERS,[6,5,4,3])) for r in ROLES}

def assign_roles_two_each(players):
    remaining={r:2 for r in ROLES}
    items=[]
    for p in players:
        allowed=list(p['allowed_roles']); random.shuffle(allowed)
        items.append((len(allowed),p['name'],allowed))
    items.sort(key=lambda x:x[0]); assignment={}
    def dfs(i):
        if i==len(items): return True
        _,name,allowed=items[i]
        cand=[r for r in allowed if remaining[r]>0]; random.shuffle(cand)
        for r in cand:
            remaining[r]-=1; assignment[name]=r
            if dfs(i+1): return True
            remaining[r]+=1; assignment.pop(name,None)
        return False
    return dict(assignment) if dfs(0) else None

def generate(players, mode, score_points, diff_limit=1, use_diff=True, recent_n=1, history=None):
    if len(players)!=8: raise ValueError('플레이어 8명을 입력하세요.')
    names=[p['name'].strip() for p in players]
    if any(not n for n in names) or len(set(names))!=8: raise ValueError('8명의 서로 다른 닉네임을 입력하세요.')
    if mode not in ('char','team'): raise ValueError('잘못된 모드입니다.')
    history=history or []
    clean=[]
    for p in players:
        name=p['name'].strip(); tiers=p.get('roles',{})
        if not isinstance(tiers,dict) or not tiers: raise ValueError(f'{name}: 캐릭터를 선택하세요.')
        if any(r not in ROLES or t not in TIERS for r,t in tiers.items()): raise ValueError(f'{name}: 티어 설정이 올바르지 않습니다.')
        allowed=list(tiers)
        if recent_n>0 and len(allowed)>1:
            banned={h[name] for h in history[-recent_n:] if name in h}
            filtered=[r for r in allowed if r not in banned]
            if filtered: allowed=filtered
        clean.append({'name':name,'allowed_roles':allowed,'role_tiers':tiers,'role_points':{r:int(score_points[r][t]) for r,t in tiers.items()}})
    # As in original: role allocation is attempted, then one set of 4v4 partitions is evaluated.
    assignment=None
    for _ in range(600):
        assignment=assign_roles_two_each(clean)
        if assignment: break
    if not assignment: raise ValueError('캐릭 배정 실패: 가능 캐릭을 늘리거나 최근 N판 제외를 줄여보세요.')
    by_role={r:[] for r in ROLES}; pmap={p['name']:p for p in clean}
    for name,role in assignment.items(): by_role[role].append(name)
    participants=[{'name':name,'role':role,'tier':pmap[name]['role_tiers'][role],'points':pmap[name]['role_points'][role]} for name,role in assignment.items()]
    if mode=='char':
        return {'mode':mode,'assignment':assignment,'participants':participants,'teams':None,'diff':None,'scores':None}
    combos=[]
    for dru in by_role['드루이드']:
      for nec in by_role['네크']:
       for ass in by_role['어쌔']:
        for smd in by_role['슴딘']:
            team_a={dru,nec,ass,smd}; team_b=set(assignment)-team_a
            sa=sum(pmap[n]['role_points'][assignment[n]] for n in team_a)
            sb=sum(pmap[n]['role_points'][assignment[n]] for n in team_b)
            combos.append((abs(sa-sb),sa,sb,team_a,team_b))
    if use_diff:
        combos=[c for c in combos if c[0]<=diff_limit]
        if not combos: raise ValueError('팀 밸런스 실패: 재생성하거나 허용 점수차를 높여보세요.')
        diffs=sorted({c[0] for c in combos}); weights=[1 if d==0 else .6 if d==1 else .4 if d==2 else .25 for d in diffs]
        chosen=random.choices(diffs,weights=weights,k=1)[0]
        combos=[c for c in combos if c[0]==chosen]
    else:
        mind=min(c[0] for c in combos); combos=[c for c in combos if c[0]==mind]
    random.shuffle(combos)
    diff,sa,sb,team_a,team_b=combos[0]
    for p in participants: p['team']='A' if p['name'] in team_a else 'B'
    return {'mode':mode,'assignment':assignment,'participants':participants,'teams':{'A':{assignment[n]:n for n in team_a},'B':{assignment[n]:n for n in team_b}},'diff':diff,'scores':{'A':sa,'B':sb}}
