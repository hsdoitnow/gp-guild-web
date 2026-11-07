"""Original PyQt team's role assignment and score-balance logic, extracted for the web."""
import random
ROLES = ['드루이드','어쌔','네크','슴딘']
TIERS = ['SSS+','SSS','SS+','SS','S+','S','A+','A']
DEFAULT_SCORES = {r:dict(zip(TIERS,[6.5,6,5.5,5,4.5,4,3.5,3])) for r in ROLES}

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

def generate(players, mode, score_points, diff_limit=1, use_diff=True, recent_n=1,
             history=None, previous_teams=None, min_team_changes=2,
             role_history=None, recent_exclusion_roles=None):
    """Find feasible teams while minimizing the number of players repeating a recent role.

    Hard requirements (role quotas, team composition, balance, and team rotation)
    are never relaxed. A recent-role exclusion is relaxed for the fewest possible
    players, rather than being switched off for everyone.
    """
    if len(players) != 8:
        raise ValueError('플레이어 8명을 입력하세요.')
    names = [p['name'].strip() for p in players]
    if any(not n for n in names) or len(set(names)) != 8:
        raise ValueError('8명의 서로 다른 닉네임을 입력하세요.')
    if mode not in ('char', 'team'):
        raise ValueError('잘못된 모드입니다.')
    role_history = role_history or {}
    recent_roles = {n: role_history.get(n, [])[:10] for n in names}
    exclusions = (recent_roles if recent_exclusion_roles is None else
                  {n: recent_exclusion_roles.get(n, []) for n in names})
    pmap = {}
    for p in players:
        name = p['name'].strip()
        tiers = p.get('roles', {})
        if not isinstance(tiers, dict) or not tiers:
            raise ValueError(f'{name}: 캐릭터를 선택하세요.')
        if any(r not in ROLES or t not in TIERS for r, t in tiers.items()):
            raise ValueError(f'{name}: 티어 설정이 올바르지 않습니다.')
        pmap[name] = {'tiers': tiers,
                      'points': {r: float(score_points[r][t]) for r, t in tiers.items()}}
    old_a = old_b = None
    if previous_teams:
        old_a = set(previous_teams['A'])
        old_b = set(previous_teams['B'])
        if old_a | old_b != set(names) or len(old_a) != 4 or len(old_b) != 4:
            old_a = old_b = None

    # Smaller violation count is always better than any fairness improvement.
    # Fairness is measured within each person's playable characters only.
    def quality(assignment):
        violations = 0
        spreads = []
        usage = 0
        for name, role in assignment.items():
            if recent_n > 0 and role in exclusions[name][:recent_n]:
                violations += 1
            counts = [recent_roles[name].count(r) + (r == role)
                      for r in pmap[name]['tiers']]
            spreads.append(max(counts) - min(counts))
            usage += recent_roles[name].count(role)
        return (violations, max(spreads), sum(spreads), usage)

    # Enumerate valid character assignments, including those that repeat a role.
    # With 8 players and 4 roles (2 each), this is small enough to avoid
    # random sampling missing a feasible zero-violation assignment.
    order = sorted(names, key=lambda n: len(pmap[n]['tiers']))
    quotas = {r: 2 for r in ROLES}
    assignment = {}
    best_key = None
    best_candidates = []

    def consider():
        nonlocal best_key, best_candidates
        key = quality(assignment)
        if best_key is not None and key > best_key:
            return
        participants = [{'name': n, 'role': assignment[n],
                         'tier': pmap[n]['tiers'][assignment[n]],
                         'points': pmap[n]['points'][assignment[n]]}
                        for n in names]
        if mode == 'char':
            candidates = [{'mode': 'char', 'assignment': dict(assignment),
                           'participants': participants, 'teams': None,
                           'diff': None, 'scores': None}]
        else:
            from itertools import product
            by_role = {r: [n for n in names if assignment[n] == r] for r in ROLES}
            candidates = []
            for selected in product(*(by_role[r] for r in ROLES)):
                team_a = set(selected)
                team_b = set(names) - team_a
                if old_a is not None:
                    if (len(team_a & old_a) > 4 - min_team_changes or
                        len(team_a & old_b) > 4 - min_team_changes or
                        len(team_b & old_a) > 4 - min_team_changes or
                        len(team_b & old_b) > 4 - min_team_changes):
                        continue
                sa = sum(pmap[n]['points'][assignment[n]] for n in team_a)
                sb = sum(pmap[n]['points'][assignment[n]] for n in team_b)
                diff = abs(sa - sb)
                if use_diff and diff > diff_limit + 1e-9:
                    continue
                candidates.append({'mode': 'team', 'assignment': dict(assignment),
                                   'participants': [dict(p, team='A' if p['name'] in team_a else 'B')
                                                    for p in participants],
                                   'teams': {'A': {assignment[n]: n for n in team_a},
                                             'B': {assignment[n]: n for n in team_b}},
                                   'diff': diff, 'scores': {'A': sa, 'B': sb},
                                   'rotation_applied': old_a is not None})
            if not use_diff and candidates:
                min_diff = min(c['diff'] for c in candidates)
                candidates = [c for c in candidates if c['diff'] <= min_diff + 1e-9]
        if not candidates:
            return
        if best_key is None or key < best_key:
            best_key = key
            best_candidates = []
        # Reservoir sampling over equal-quality candidates avoids keeping
        # a potentially large list of equivalent combinations.
        for candidate in candidates:
            if len(best_candidates) < 64:
                best_candidates.append(candidate)
            else:
                j = random.randrange(len(best_candidates) + 1)
                if j < len(best_candidates):
                    best_candidates[j] = candidate

    def dfs(i):
        if i == len(order):
            consider()
            return
        name = order[i]
        options = list(pmap[name]['tiers'])
        random.shuffle(options)
        for role in options:
            if quotas[role] == 0:
                continue
            quotas[role] -= 1
            assignment[name] = role
            dfs(i + 1)
            quotas[role] += 1
            del assignment[name]

    dfs(0)
    if best_candidates:
        result = random.choice(best_candidates)
        result['recent_role_exceptions'] = best_key[0]
        return result
    if old_a is not None:
        raise ValueError('팀별 2명 교환 및 점수 조건을 만족하는 조합이 없습니다. 점수차 제한을 완화하거나 팀 교환 옵션을 해제하세요.')
    raise ValueError('팀 배정 실패: 가능 캐릭터나 허용 점수차 조건을 완화해 주세요.')
