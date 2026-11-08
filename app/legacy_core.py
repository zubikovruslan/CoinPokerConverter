#!/usr/bin/env python3
"""Conservative CoinPoker -> PokerStars-text converter for H2N3.

Amounts are integer cents. USD is a display label, with no currency conversion.
Unrepresentable or inconsistent hands are retained separately, never invented.
"""
from __future__ import annotations
import argparse
import collections
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

MONEY = r'(\d+(?:\.\d+)?)'

def cents(s):
    a, _, b = s.partition('.')
    assert len(b) <= 2, s
    return int(a) * 100 + int((b + '00')[:2])

def money(n):
    return f'{n // 100}.{n % 100:02d}'

def read_hands(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    blocks = re.split(r'(?=^CoinPoker Hand #)', text, flags=re.M)
    assert not blocks[0].strip(), 'Unrecognized text before first hand'
    return [parse_hand(b.strip()) for b in blocks[1:]]

def parse_hand(text):
    lines = text.splitlines()
    m = re.fullmatch(r'CoinPoker Hand #(\d+): NLH \(₮' + MONEY + r'/₮' + MONEY + r'(?:/₮' + MONEY + r')?\) (\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) MSK', lines[0])
    if not m: raise ValueError('Unknown header: ' + lines[0])
    hid, sb, bb, ante, date = m.groups()
    t = re.fullmatch(r"Table '([^']+)' (\d+)-max Seat #(\d+) is the button", lines[1])
    h = dict(id=hid, sb=cents(sb), bb=cents(bb), ante=cents(ante or '0'), date=date,
             table=t[1], max_seats=int(t[2]), button=int(t[3]), text=text,
             players={}, events=[], boards={}, known_cards={}, folded={},
             collected=collections.defaultdict(int), contributed=collections.defaultdict(int),
             features=set(), errors=[], splash=0, cashouts={}, summaries={})
    street = collections.defaultdict(int)
    stage = 'PREFLOP'; run = 1; summary = False; started = False
    for line in lines[2:]:
        if line == '*** SUMMARY ***': summary = True; continue
        if summary:
            if m := re.fullmatch(r'Total pot ₮'+MONEY+r' \| Rake ₮'+MONEY+r'(?: \| Splash Fee ₮'+MONEY+r')?', line):
                h['pot'], h['rake'], h['fee'] = [cents(x or '0') for x in m.groups()]
            elif m := re.fullmatch(r'(?:(FIRST|SECOND|THIRD) )?Board \[\s*(.*?)\s*\]', line):
                h['boards'][{None:1,'FIRST':1,'SECOND':2,'THIRD':3}[m[1]]] = m[2].split()
            elif m := re.fullmatch(r'Seat (\d+): (\S+) (.+)', line):
                h['summaries'][m[2]] = m[3]
            elif line.startswith(('Hand was run ', 'Game ended:')): pass
            elif line.strip(): raise ValueError((hid, 'Unknown summary', line))
            continue
        if m := re.fullmatch(r'Seat (\d+): (\S+) \(₮'+MONEY+r' in chips\)', line):
            h['players'][m[2]] = {'seat':int(m[1]), 'stack':cents(m[3])}; continue
        if line == '*** HOLE CARDS ***':
            started=True; h['events'].append({'kind':'hole'}); continue
        if m := re.fullmatch(r'Dealt to (\S+)(?: \[(.*?)\])?', line):
            if m[2]:
                cards=m[2].split(); h['known_cards'][m[1]]=cards
                h['events'].append({'kind':'dealt','player':m[1],'cards':cards})
            continue
        if m := re.fullmatch(r'(?:(MEGA) )?SPLASH dropped ₮'+MONEY, line):
            h['features'].add('splash'); h['splash'] += cents(m[2]); continue
        if m := re.fullmatch(r'\*\*\* (?:(FIRST|SECOND|THIRD) )?(FLOP|TURN|RIVER|SHOWDOWN) \*\*\*(.*)', line):
            run={None:1,'FIRST':1,'SECOND':2,'THIRD':3}[m[1]]
            stage=m[2]
            if run>1: h['features'].add('run'+str(run))
            if stage != 'SHOWDOWN': street=collections.defaultdict(int)
            cards=[x.split() for x in re.findall(r'\[([^]]+)\]', m[3])]
            h['events'].append({'kind':'street','stage':stage,'run':run,'cards':cards})
            continue
        if m := re.fullmatch(r'(\S+) collected ₮'+MONEY+r' from pot', line):
            p,n=m[1],cents(m[2]); h['collected'][p]+=n
            h['events'].append({'kind':'collect','player':p,'amount':n,'run':run}); continue
        if m := re.fullmatch(r'(\S+) cashed out the hand for ₮'+MONEY+r' \| Cash Out Fee ₮'+MONEY, line):
            h['cashouts'][m[1]]={'amount':cents(m[2]),'fee':cents(m[3])}
            h['events'].append({'kind':'cashout','player':m[1],'amount':cents(m[2]),'fee':cents(m[3])})
            h['features'].add('cashout'); continue
        m=re.fullmatch(r'(\S+): (.+)',line)
        if not m: raise ValueError((hid,'Unknown line',line))
        p, action=m.groups()
        assert p in h['players'], (hid,p)
        e={'player':p,'stage':stage,'run':run}
        before=h['contributed'][p]; current=street[p]; high=max(street.values(),default=0)
        if action in ('folds','checks','mucks hand'):
            kind={'folds':'fold','checks':'check','mucks hand':'muck'}[action]
            e['kind']=kind
            if kind=='fold': h['folded'][p]=stage
            if kind=='check' and current<high: h['errors'].append(['check_facing_bet',p,stage,high-current])
        elif m := re.fullmatch(r'shows \[([^]]+)\] \((.+)\)',action):
            e.update(kind='show',cards=m[1].split(),description=m[2]);
            if p in h['known_cards'] and h['known_cards'][p]!=e['cards']: h['errors'].append(['changed_hole_cards',p])
            h['known_cards'][p]=e['cards']
        elif m := re.fullmatch(r'RETURN ₮'+MONEY, action):
            n=cents(m[1]); e.update(kind='return',amount=n)
            street[p]-=n; h['contributed'][p]-=n
            if street[p]<0: h['errors'].append(['invalid_return',p,stage,n])
        else:
            delta=0
            if m := re.fullmatch(r'posts (small blind|big blind|auto big blind|ante) ₮'+MONEY+r'( ALLIN)?',action):
                kind={'small blind':'sb','big blind':'bb','auto big blind':'auto_bb','ante':'ante'}[m[1]]
                delta=cents(m[2]); e.update(kind=kind,amount=delta)
                if kind=='auto_bb': h['features'].add('auto_blind')
                if kind=='ante': h['features'].add('ante')
                else: street[p]+=delta
            elif m := re.fullmatch(r'STRADDLE ₮'+MONEY,action):
                delta=cents(m[1]); e.update(kind='straddle',amount=delta)
                street[p]+=delta;h['features'].add('straddle')
            elif m := re.fullmatch(r'raises ₮'+MONEY+r' to ₮'+MONEY,action):
                inc,target=map(cents,m.groups());delta=target-current
                e.update(kind='raise',amount=delta,target=target,increment=inc)
                if inc!=target-high: h['errors'].append(['raise_increment',p,inc,target-high])
                if delta<0: h['errors'].append(['negative_raise',p,delta])
                street[p]=target
            elif m := re.fullmatch(r'(calls|bets|ALLIN) ₮'+MONEY,action):
                typ=m[1];delta=cents(m[2]);target=current+delta
                if typ=='ALLIN':
                    h['features'].add('allin')
                    kind='bet' if high==0 else ('raise' if target>high else 'call')
                    e['explicit_allin']=True
                else:kind={'calls':'call','bets':'bet'}[typ]
                e.update(kind=kind,amount=delta)
                if kind=='raise':e.update(target=target,increment=target-high)
                if typ=='ALLIN' and kind=='raise':
                    can_match_raise=any(q!=p and q not in h['folded'] and
                        street[q]+v['stack']-h['contributed'][q]>high
                        for q,v in h['players'].items())
                    if not can_match_raise:
                        # An untyped ALLIN may contain an amount which no opponent
                        # can match. Its fully returned excess is not a real raise.
                        e['effective_call']=high-current
                        e['unmatchable_excess']=target-high
                        h['features'].add('allin_excess_no_raise')
                if kind=='call' and target>high:h['errors'].append(['overcall',p,stage,target,high])
                if kind=='bet' and high>0:h['errors'].append(['bet_facing_bet',p,stage])
                street[p]=target
            else: raise ValueError((hid,'Unknown action',action))
            h['contributed'][p]+=delta
            left=h['players'][p]['stack']-h['contributed'][p]
            e['allin']=left==0
            if e['kind']=='call' and street[p]<high and left!=0:
                h['errors'].append(['undercall_without_allin',p,stage,high-street[p]])
            if left<0:h['errors'].append(['stack_overdraw',p,left,line])
            if e.get('explicit_allin') and left!=0:h['errors'].append(['allin_not_full_stack',p,left])
        h['events'].append(e)
    h['net_contributions']=sum(h['contributed'].values())
    h['total_collected']=sum(h['collected'].values())
    h['pot_difference']=h['pot']-h['net_contributions']-h['splash']
    h['payout_difference']=h['pot']-h['total_collected']-h['rake']-h['fee']
    h['hero_net']=h['collected'].get('Hero',0)-h['contributed'].get('Hero',0)
    assert len(h['known_cards'].get('Hero',[]))==2, hid
    assert len(h['players'])>=2, hid
    return h

def exclusions(h):
    reasons=[]
    if h['errors']:reasons.append('source_action_inconsistency')
    if h['pot_difference']:reasons.append('source_pot_inconsistency')
    if h['splash']:reasons.append('splash_external_money_not_representable_in_standard_pokerstars')
    if h['payout_difference']!=h['splash']:reasons.append('source_payout_does_not_balance')
    if 'run3' in h['features']:reasons.append('three_boards_no_confirmed_h2n3_pokerstars_representation')
    return reasons

def convert_hand(h):
    assert not exclusions(h), (h['id'],exclusions(h))
    et=datetime.strptime(h['date'],'%Y/%m/%d %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Moscow')).astimezone(ZoneInfo('America/New_York')).strftime('%Y/%m/%d %H:%M:%S')
    out=[f"PokerStars Hand #{h['id']}: Hold'em No Limit (${money(h['sb'])}/${money(h['bb'])} USD) - {h['date']} MSK [{et} ET]",
         f"Table 'CoinPoker_{h['table']}' {h['max_seats']}-max Seat #{h['button']} is the button"]
    for p,v in sorted(h['players'].items(),key=lambda kv:kv[1]['seat']):
        out.append(f"Seat {v['seat']}: {p} (${money(v['stack'])} in chips)")
    roles=collections.defaultdict(list)
    for p,v in h['players'].items():
        if v['seat']==h['button']:roles[p].append('button')
    for e in h['events']:
        if e['kind']=='sb':roles[e['player']].append('small blind')
        if e['kind']=='bb':roles[e['player']].append('big blind')
    showdown=len(set(h['players'])-set(h['folded']))>=2
    second_streets={e['stage'] for e in h['events'] if e['kind']=='street' and e['run']==2}
    straddles=[e for e in h['events'] if e['kind']=='straddle']
    seen_voluntary=False
    shown_per_run={}
    payouts_per_run=collections.defaultdict(int)
    pending_excess={}
    for e in h['events']:
        if e['kind']=='collect':payouts_per_run[e['player'],e['run']]+=e['amount']
    for e in h['events']:
        k=e['kind'];p=e.get('player');a=money(e.get('amount',0))
        suffix=' and is all-in' if e.get('allin') else ''
        if k=='hole':
            out.extend(f"{s['player']}: posts straddle ${money(s['amount'])}" for s in straddles)
            out.append('*** HOLE CARDS ***')
        elif k=='dealt':out.append(f"Dealt to {p} [{' '.join(e['cards'])}]")
        elif k in ('sb','bb','auto_bb','ante'):
            word={'sb':'small blind','bb':'big blind','auto_bb':'big blind','ante':'the ante'}[k]
            out.append(f'{p}: posts {word} ${a}{suffix}')
        elif k=='straddle':
            assert not seen_voluntary, (h['id'],'Straddle after voluntary action')
        elif k=='street':
            stage=e['stage'];run=e['run']
            prefix=('FIRST ' if run==1 else 'SECOND ') if stage in second_streets else ''
            if stage=='SHOWDOWN':
                if showdown:out.append(f'*** {prefix}SHOW DOWN ***')
            else:
                out.append(f"*** {prefix}{stage} *** " + ' '.join('['+' '.join(cards)+']' for cards in e['cards']))
        elif k in ('fold','check','muck'):
            if k!='muck':seen_voluntary=True
            out.append(f"{p}: "+{'fold':'folds','check':'checks','muck':'mucks hand'}[k])
        elif k in ('call','bet','raise'):
            seen_voluntary=True
            if 'effective_call' in e:
                assert p not in pending_excess
                pending_excess[p]=(e['unmatchable_excess'],e['stage'])
                out.append(f"{p}: calls ${money(e['effective_call'])}")
            elif k=='raise':out.append(f"{p}: raises ${money(e['increment'])} to ${money(e['target'])}{suffix}")
            else:out.append(f"{p}: {'calls' if k=='call' else 'bets'} ${a}{suffix}")
        elif k=='return':
            if p in pending_excess:
                assert pending_excess.pop(p)==(e['amount'],e['stage']), (h['id'],p,'Excess was not fully returned')
            else:out.append(f'Uncalled bet (${a}) returned to {p}')
        elif k=='show':
            key=(p,e['run'])
            if key in shown_per_run:
                assert shown_per_run[key]['cards']==e['cards'] and shown_per_run[key]['description']==e['description']
            else:
                out.append(f"{p}: shows [{' '.join(e['cards'])}] ({e['description']})")
                shown_per_run[key]=e
        elif k=='collect':out.append(f'{p} collected ${a} from pot')
        elif k=='cashout':out.append(f"{p} cashed out the hand for ${a} | Cash Out Fee ${money(e['fee'])}")
        else:raise ValueError(k)
    assert not pending_excess, (h['id'],'Missing unmatchable excess return')
    out.append('*** SUMMARY ***')
    out.append(f"Total pot ${money(h['pot'])} | Rake ${money(h['rake']+h['fee'])}")
    if 'run2' in h['features']:out.append('Hand was run twice')
    for run,cards in sorted(h['boards'].items()):
        if cards:
            prefix=('FIRST ' if run==1 else 'SECOND ') if 'run2' in h['features'] else ''
            out.append(f"{prefix}Board [{' '.join(cards)}]")
    shown={e['player'] for e in h['events'] if e['kind']=='show'}
    for p,v in sorted(h['players'].items(),key=lambda kv:kv[1]['seat']):
        label=f"Seat {v['seat']}: {p}"+''.join(f' ({role})' for role in roles[p])+' '
        if p in h['folded']:
            stage=h['folded'][p]
            if stage=='PREFLOP':
                result='folded before Flop'
                if not h['contributed'].get(p,0):result+=" (didn't bet)"
            else:result='folded on the '+stage.title()
        elif not showdown:
            result=f"collected (${money(h['collected'].get(p,0))})"
        elif p in h['cashouts']:
            result=h['summaries'][p].replace('₮','$')
        elif p in shown:
            result=f"showed [{' '.join(h['known_cards'][p])}]"
            parts=[]
            for run in sorted(h['boards']):
                shown_event=shown_per_run.get((p,run))
                assert shown_event, (h['id'],p,run,'Missing showdown cards')
                n=payouts_per_run[p,run]
                desc=shown_event['description']
                parts.append((f'and won (${money(n)})' if n else 'and lost')+f' with {desc}')
            result+=' '+', '.join(parts)
        else:
            result='mucked'
        out.append(label+result)
    return '\n'.join(out)
