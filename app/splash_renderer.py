"""EXPERIMENT: original v1.1 rendering with external-cash-drop marker.
Known H2N3 limitation: marker did not initialize the replay pot in prior test.
Entry validation, cash-drop output, and exact duplicate cashout lines differ.
"""
import collections
from datetime import datetime
from zoneinfo import ZoneInfo
from .legacy_core import money

def assert_valid_corrected_splash(h):
    assert h['splash'] > 0 and not h['errors']
    assert h['pot'] == sum(h['contributed'].values()) + h['splash']
    assert h['pot'] == sum(h['collected'].values()) + h['rake'] + h['fee']
    assert 'Hero' not in h['cashouts'] and 'run3' not in h['features']
    if h['cashouts']:
        assert h.get('cashout_hero_result_verified'), (h['id'],'Cashout Hero result not validated')

def render_ps_splash_candidate(h):
    assert_valid_corrected_splash(h)
    et=datetime.strptime(h['date'],'%Y/%m/%d %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Moscow')).astimezone(ZoneInfo('America/New_York')).strftime('%Y/%m/%d %H:%M:%S')
    out=[f"PokerStars Hand #{h['id']}: Hold'em No Limit (${money(h['sb'])}/${money(h['bb'])} USD) - {h['date']} MSK [{et} ET]",
         f"Table 'CoinPoker_{h['table']}' {h['max_seats']}-max Seat #{h['button']} is the button"]
    for p,v in sorted(h['players'].items(),key=lambda kv:kv[1]['seat']):
        out.append(f"Seat {v['seat']}: {p} (${money(v['stack'])} in chips)")
    out.append(f"Cash Drop to Pot : total ${money(h['splash'])}")
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
    cashouts_seen={}
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
        elif k=='cashout':
            if p in cashouts_seen:
                assert cashouts_seen[p] == e, (h['id'],p,'Conflicting cashout entries')
            else:
                out.append(f"{p} cashed out the hand for ${a} | Cash Out Fee ${money(e['fee'])}")
                cashouts_seen[p]=e
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
