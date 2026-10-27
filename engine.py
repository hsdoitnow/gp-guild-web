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

def generate(players, mode, score_points, diff_limit=1, use_diff=True, recent_n=1, history=None, previous_teams=None, min_team_changes=2, role_history=None):
    if len(players)!=8: raise ValueError('플레이어 8명을 입력하세요.')
    names=[p['name'].strip() for p in players]
    if any(not n for n in names) or len(set(names))!=8: raise ValueError('8명의 서로 다른 닉네임을 입력하세요.')
    if mode not in ('char','team'): raise ValueError('잘못된 모드입니다.')
    history=history or []
    role_history=role_history or {}
    # Per-player 10 most recent completed matches, newest first.
    recent_roles={name:role_history.get(name,[])[:10] for name in names}
    clean=[]
    for p in players:
        name=p['name'].strip(); tiers=p.get('roles',{})
        if not isinstance(tiers,dict) or not tiers: raise ValueError(f'{name}: 캐릭터를 선택하세요.')
        if any(r not in ROLES or t not in TIERS for r,t in tiers.items()): raise ValueError(f'{name}: 티어 설정이 올바르지 않습니다.')
        allowed=list(tiers)
        if recent_n>0 and len(allowed)>1:
            banned=set(recent_roles[name][:recent_n])
            filtered=[r for r in allowed if r not in banned]
            if filtered: allowed=filtered
        clean.append({'name':name,'allowed_roles':allowed,'role_tiers':tiers,'role_points':{r:int(score_points[r][t]) for r,t in tiers.items()}})
    # A/B labels are arbitrary: require a real 2+2 mix of the previous teams.
    previous_teams=previous_teams or None
    if previous_teams:
        old_a=set(previous_teams['A']);old_b=set(previous_teams['B'])
        if old_a|old_b != set(names) or len(old_a)!=4 or len(old_b)!=4:
            previous_teams=None  # Different 8-player roster: no forced rotation.
    pmap={p['name']:p for p in clean}
    best=None
    # Keep the original hard constraints; prefer assignments that distribute roles
    # evenly across each player’s last ten matches.
    for _ in range(1200):
        assignment=assign_roles_two_each(clean)
        if not assignment: break
        by_role={r:[] for r in ROLES}
        for name,role in assignment.items(): by_role[role].append(name)
        participants=[{'name':name,'role':role,'tier':pmap[name]['role_tiers'][role],'points':pmap[name]['role_points'][role]} for name,role in assignment.items()]
        diversity=sum(recent_roles[name].count(role) + (10 if recent_n and role in recent_roles[name][:recent_n] else 0) for name,role in assignment.items())
        if mode=='char':
            candidate={'mode':mode,'assignment':assignment,'participants':participants,'teams':None,'diff':None,'scores':None}
            if best is None or diversity<best[0]: best=(diversity,candidate)
            if diversity==0: break
            continue
        combos=[]
        for dru in by_role['드루이드']:
          for nec in by_role['네크']:
           for ass in by_role['어쌔']:
            for smd in by_role['슴딘']:
                team_a={dru,nec,ass,smd};team_b=set(assignment)-team_a
                if previous_teams:
                    # Each new team must have at least 2 players from EACH previous team.
                    # This also rejects relabeling A/B without mixing players.
                    if len(team_a & old_a)>4-min_team_changes or len(team_a & old_b)>4-min_team_changes: continue
                    if len(team_b & old_a)>4-min_team_changes or len(team_b & old_b)>4-min_team_changes: continue
                sa=sum(pmap[n]['role_points'][assignment[n]] for n in team_a)
                sb=sum(pmap[n]['role_points'][assignment[n]] for n in team_b)
                combos.append((abs(sa-sb),sa,sb,team_a,team_b))
        if not combos: continue
        if use_diff:
            combos=[c for c in combos if c[0]<=diff_limit]
            if not combos: continue
            diffs=sorted({c[0] for c in combos}); weights=[1 if d==0 else .6 if d==1 else .4 if d==2 else .25 for d in diffs]
            chosen=random.choices(diffs,weights=weights,k=1)[0]
            combos=[c for c in combos if c[0]==chosen]
        else:
            mind=min(c[0] for c in combos);combos=[c for c in combos if c[0]==mind]
        random.shuffle(combos)
        diff,sa,sb,team_a,team_b=combos[0]
        for p in participants: p['team']='A' if p['name'] in team_a else 'B'
        candidate={'mode':mode,'assignment':assignment,'participants':participants,'teams':{'A':{assignment[n]:n for n in team_a},'B':{assignment[n]:n for n in team_b}},'diff':diff,'scores':{'A':sa,'B':sb},'rotation_applied':bool(previous_teams)}
        # The score difference is already constrained above. Diversity decides
        # between feasible assignments; equal diversity keeps some randomness.
        if best is None or diversity<best[0] or (diversity==best[0] and random.random()<0.15): best=(diversity,candidate)
        if diversity==0: break
    if best is not None: return best[1]
    if recent_n>0:
        # Strict recent-role exclusions can make 2-per-role + team rotation
        # impossible. Relax only that exclusion; retain a heavy penalty.
        return generate(players,mode,score_points,diff_limit,use_diff,0,history,previous_teams,min_team_changes,role_history)
    if previous_teams:
        raise ValueError('직전 경기 대비 팀별 2명 이상 변경하면서 캐릭터/점수 조건을 만족하는 조합이 없습니다. 점수차 제한을 완화하거나 옵션을 해제하세요.')
    raise ValueError('팀 배정 실패: 가능 캐릭터나 허용 점수차 조건을 완화해 주세요.')
