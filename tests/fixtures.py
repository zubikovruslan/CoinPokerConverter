"""Entirely synthetic histories; no real player data."""


def regular(hid="900001", hero="Hero"):
    return f"""CoinPoker Hand #{hid}: NLH (₮0.05/₮0.10) 2026/11/01 12:00:00 MSK
Table 'Synthetic' 6-max Seat #1 is the button
Seat 1: {hero} (₮10.00 in chips)
Seat 2: Rival (₮10.00 in chips)
{hero}: posts small blind ₮0.05
Rival: posts big blind ₮0.10
*** HOLE CARDS ***
Dealt to {hero} [As Kd]
{hero}: folds
Rival: RETURN ₮0.05
Rival collected ₮0.10 from pot
*** SUMMARY ***
Total pot ₮0.10 | Rake ₮0.00 | Splash Fee ₮0.00
Board []
Seat 1: {hero} folded before Flop
Seat 2: Rival collected (₮0.10)
Game ended: 2026/11/01 12:00:03 MSK"""


def splash(hid="900010", cashout=False, hero_cashout=False):
    c = ("Hero" if hero_cashout else "Rival") + " cashed out the hand for ₮1.96 | Cash Out Fee ₮0.06\n" if cashout else ""
    return f"""CoinPoker Hand #{hid}: NLH (₮0.01/₮0.02) 2026/11/01 12:01:00 MSK
Table 'Synthetic' 6-max Seat #1 is the button
Seat 1: Hero (₮2.00 in chips)
Seat 2: Rival (₮2.00 in chips)
Hero: posts small blind ₮0.01
Rival: posts big blind ₮0.02
*** HOLE CARDS ***
Dealt to Hero [As Ah]
SPLASH dropped ₮2.00
Hero: ALLIN ₮1.99
Rival: ALLIN ₮1.98
{c}*** FLOP *** [2c 3d 7h]
*** TURN *** [2c 3d 7h] [9s]
*** RIVER *** [2c 3d 7h 9s] [Jc]
*** SHOWDOWN ***
Hero: shows [As Ah] (a pair of Aces)
Rival: shows [Ks Kh] (a pair of Kings)
Hero collected ₮3.80 from pot
*** SUMMARY ***
Total pot ₮6.00 | Rake ₮0.20 | Splash Fee ₮0.00
Board [2c 3d 7h 9s Jc]
Seat 1: Hero showed [As Ah] and won (₮3.80)
Seat 2: Rival showed [Ks Kh] and lost
Game ended: 2026/11/01 12:01:20 MSK"""


def neighbor():
    return regular("900011").replace("₮0.05/₮0.10", "₮0.01/₮0.02").replace("12:00:00", "12:01:21").replace("12:00:03", "12:01:24").replace("Hero (₮10.00", "Hero (₮5.80").replace("₮0.05", "₮0.01").replace("₮0.10", "₮0.02")
