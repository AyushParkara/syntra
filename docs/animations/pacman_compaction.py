#!/usr/bin/env python3
"""Pac-Man compaction animation — iconic, instantly readable.

Pac-Man = Syntra · pellets = messages · power-pellet = compaction trigger.
He chomps across the row eating messages, eats the big ◉, the screen flashes,
and everything collapses into one compact █ memory block. Classic + legible.

Run:  python3 pacman_compaction.py        (once)
      python3 pacman_compaction.py loop
"""
import sys, time

W, H = 50, 7
FPS = 18
RST="\x1b[0m"; HIDE="\x1b[?25l"; SHOW="\x1b[?25h"; HOME="\x1b[H"; CLR="\x1b[2J"
def rgb(c): return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
YEL=(255,221,0); DIM=(120,120,140); WHT=(240,240,250); BLU=(70,160,255); PNK=(255,120,200); GRN=(120,235,140); GOLD=(255,200,70)

# Classic Pac-Man mouth cycle (facing right): wide → half → closed → half
PAC = ["C", "ᑦ", "●", "ᑦ"]      # open-wide, half, closed, half  (chomp rhythm)
# pellet glyphs
PELLET="•"; POWER="◉"

class Canvas:
    def __init__(s): s.clear()
    def clear(s): s.ch=[[" "]*W for _ in range(H)]; s.co=[[DIM]*W for _ in range(H)]
    def put(s,x,y,text,col):
        if y<0 or y>=H: return
        for i,c in enumerate(text):
            xx=x+i
            if 0<=xx<W: s.ch[y][xx]=c; s.co[y][xx]=col
    def render(s,status,scol=WHT):
        buf=[HOME,"  "+rgb(scol)+status+RST+" "*max(0,W-len(status))]
        buf.append("  "+rgb(DIM)+"╭"+"─"*W+"╮"+RST)
        for y in range(H):
            row=["  "+rgb(DIM)+"│"]; cur=None
            for x in range(W):
                co=s.co[y][x]
                if co!=cur: row.append(rgb(co)); cur=co
                row.append(s.ch[y][x])
            row.append(RST+rgb(DIM)+"│"+RST); buf.append("".join(row))
        buf.append("  "+rgb(DIM)+"╰"+"─"*W+"╯"+RST)
        sys.stdout.write("\n".join(buf)+"\n"); sys.stdout.flush()

ROW = 3                 # the lane Pac-Man runs along
TRACK_X0 = 2

def play():
    cv=Canvas(); d=1.0/FPS
    def show(status,scol=WHT): cv.render(status,scol); time.sleep(d)

    n_pellets = 30                      # messages as pellets
    power_x = TRACK_X0 + n_pellets + 1  # the big power-pellet at the end

    # 1) CHOMP across the lane — pellets vanish as Pac passes; mouth animates
    for px in range(TRACK_X0, power_x+1):
        cv.clear()
        # remaining pellets (those ahead of Pac)
        for i in range(n_pellets):
            gx = TRACK_X0 + i
            if gx > px:
                cv.put(gx, ROW, PELLET, YEL if i%5 else GOLD)
        # the power pellet (blinks) if not yet reached
        if px < power_x and (px % 2 == 0):
            cv.put(power_x, ROW, POWER, GOLD)
        # Pac-Man, mouth chomping
        mouth = PAC[px % len(PAC)]
        cv.put(px, ROW, mouth, YEL)
        # progress label
        eaten = px - TRACK_X0
        cv.put(2, H-1, f"compacting…  {min(100,int(eaten/n_pellets*100))}%", DIM)
        show("compacting conversation")

    # 2) POWER-UP — Pac eats ◉, grows, screen pulses (the "spell" moment)
    for f in range(8):
        cv.clear()
        big = "◖█◗" if f%2==0 else "◖▣◗"          # chunky powered-up Pac
        col = WHT if f%2==0 else YEL
        cv.put(power_x-1, ROW, big, col)
        # radiating pulse
        for r in (2,4,6):
            if f >= r//2:
                cv.put(power_x-1-r, ROW, "‹", col); cv.put(power_x+1+r, ROW, "›", col)
        cv.put(2, H-1, "POWER PELLET — compressing!", GOLD)
        show("compacting conversation", GOLD)

    # 3) COLLAPSE — everything sweeps inward into a single compact block
    center = W//2
    for f in range(10):
        cv.clear()
        reach = max(1, 20 - f*2)
        # two walls of '█' sweeping toward center (trash-compactor feel, Pac-themed)
        left = center - reach; right = center + reach
        cv.put(left, ROW, "▐"+"█"*max(0,(center-left-1)), BLU)
        cv.put(center, ROW, "█", GOLD)
        cv.put(center+1, ROW, "█"*max(0,(right-center-1))+"▌", PNK)
        cv.put(2, H-1, "collapsing context…", DIM)
        show("compacting conversation")

    # 4) RESULT — one neat memory block + stats (no flicker, holds)
    for f in range(30):
        cv.clear()
        bx = center-7
        cv.put(bx, ROW-1, "┌"+"─"*12+"┐", GOLD)
        cv.put(bx, ROW,   "│ memory.pkg │", lerp(GOLD,WHT,0.4))
        cv.put(bx, ROW+1, "└"+"─"*12+"┘", GOLD)
        # tiny Pac sitting next to it, content
        cv.put(bx-4, ROW, "ᑦ", YEL)
        cv.put(2, H-1, "30 messages → 1 block · 128k→7.4k tokens · 98% kept", DIM)
        show("done", GRN)

def lerp(a,b,t): return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))

def main():
    sys.stdout.write(HIDE+CLR)
    try:
        loop=len(sys.argv)>1 and sys.argv[1]=="loop"
        while True:
            play()
            if not loop: break
            time.sleep(0.5)
    except KeyboardInterrupt: pass
    finally: sys.stdout.write(SHOW+RST+"\n")

if __name__=="__main__": main()
