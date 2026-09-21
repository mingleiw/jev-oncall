import json
from PIL import Image, ImageDraw, ImageFont

S = 2  # scale
W = 1200
PAD = 40
BG = (13,17,23); PANEL = (22,27,34); BORDER = (48,54,61)
TEXT = (230,237,243); GRAY = (139,148,158); BLUE = (121,192,255)
GREEN = (63,185,80); RED = (248,81,73); AMBER = (210,153,34); PURPLE = (188,140,255)
SEVC = {'SEV1': RED, 'SEV2': AMBER, 'SEV3': BLUE, 'SEV4': GRAY}

FD = '/usr/share/fonts/truetype/dejavu/'
def F(name, size): return ImageFont.truetype(FD + name, size * S)
f_title = F('DejaVuSans-Bold.ttf', 30); f_sub = F('DejaVuSans.ttf', 14)
f_bign = F('DejaVuSansMono-Bold.ttf', 26); f_lab = F('DejaVuSans.ttf', 12)
f_b = F('DejaVuSans-Bold.ttf', 14); f_t = F('DejaVuSans.ttf', 12.5)
f_small = F('DejaVuSans.ttf', 11.5); f_mono = F('DejaVuSansMono.ttf', 11.5)
f_chip = F('DejaVuSans-Bold.ttf', 11); f_chipn = F('DejaVuSansMono-Bold.ttf', 12)

base = '/home/hatch/workspace/jev-incident-triage'
results = json.load(open(base + '/results.json'))['results']
alerts = {a['id']: a for a in json.load(open(base + '/alerts.json'))}

img = Image.new('RGB', (W * S, 4000 * S), BG)
d = ImageDraw.Draw(img)
y = 36

def rr(x0, y0, x1, y1, fill, outline=None, r=8, width=1):
    d.rounded_rectangle([x0*S, y0*S, x1*S, y1*S], radius=r*S, fill=fill,
                        outline=outline, width=max(1, int(width*S)))

def txt(x, y, s, font, fill=TEXT):
    d.text((x*S, y*S), s, font=font, fill=fill)

def tw(s, font):
    b = d.textbbox((0,0), s, font=font); return (b[2]-b[0])/S

def wrap(s, font, maxw):
    words, lines, cur = s.split(), [], ''
    for w_ in words:
        t = (cur + ' ' + w_).strip()
        if tw(t, font) <= maxw: cur = t
        else:
            if cur: lines.append(cur)
            cur = w_
    if cur: lines.append(cur)
    return lines

def badge(x, y, label, fg, bg=None, dashed=False):
    pad_x, h = 7, 22
    w_ = tw(label, f_chip) + pad_x*2
    if bg: rr(x, y, x+w_, y+h, bg, fg, r=5)
    else: rr(x, y, x+w_, y+h, PANEL, fg, r=5)
    if dashed:
        # dashed outline approximation: draw short segments
        for i in range(0, int(w_), 8):
            d.line([(x+i)*S, (y+h)*S, (x+min(i+5,int(w_)))*S, (y+h)*S], fill=fg, width=S)
            d.line([(x+i)*S, y*S, (x+min(i+5,int(w_)))*S, y*S], fill=fg, width=S)
    txt(x+pad_x, y+3.5, label, f_chip, fg)
    return w_

# header
txt(PAD, y, 'jev', f_title, BLUE); txt(PAD + tw('jev', f_title), y, '-oncall', f_title)
y += 44
txt(PAD, y, 'One Jev call per alert  ·  4 typed questions  ·  zero prose to parse  ·  model jev-1.13.0', f_sub, GRAY)
y += 30

# stats
stats = [('14', 'alerts triaged', TEXT), ('9.2s', 'total · ~658ms per alert', TEXT),
         ('$0.0005', '12,625 input tokens · output free', TEXT), ('5', 'routed to human review (low confidence)', AMBER)]
cw = (W - PAD*2 - 36) / 4
for i, (n, l, c) in enumerate(stats):
    x = PAD + i*(cw+12)
    rr(x, y, x+cw, y+86, PANEL, BORDER, r=10)
    txt(x+16, y+12, n, f_bign, c)
    for j, ln in enumerate(wrap(l, f_lab, cw-32)):
        txt(x+16, y+48+j*16, ln, f_lab, GRAY)
y += 86 + 16

# flow strip
flow = [('alert', 'title + description'), ('Jev', '4 questions, 1 call'),
        ('route', 'plain code'), ('confidence gate', '< 0.75 → human')]
fx = PAD
parts = []
for name, sub in flow:
    parts.append((name, sub))
# draw as single strip with segments
strip_h = 58
rr(PAD, y, W-PAD, y+strip_h, PANEL, BORDER, r=10)
sx = PAD + 18
for k, (name, sub) in enumerate(flow):
    if k: txt(sx, y+17, '→', f_b, BLUE); sx += tw('→', f_b) + 14
    txt(sx, y+8, name, f_b); txt(sx, y+30, sub, f_small, GRAY)
    sx += max(tw(name, f_b), tw(sub, f_small)) + 26
y += strip_h + 16

# question chips
chips = [('actionable?', 'Noul'), ('severity?  SEV1–SEV4', 'Choice'),
         ('team?  db / compute / network / deploy', 'Choice'), ('duplicate?', 'Noul')]
cx = PAD
for label, typ in chips:
    w_ = tw(label, f_chipn) + tw('  ' + typ, f_small) + 30
    rr(cx, y, cx+w_, y+34, PANEL, BORDER, r=8)
    txt(cx+12, y+8, label, f_chipn, BLUE)
    txt(cx+12+tw(label, f_chipn)+6, y+9, typ, f_small, GRAY)
    cx += w_ + 8
y += 34 + 18

# cards
def card_spec(r):
    a = alerts[r['id']]
    desc = a.get('description', '')[:160]
    lines = wrap(desc, f_t, (W-PAD*2-12)/2 - 32)[:3]
    h = 14 + 26 + 8 + 20 + 6 + len(lines)*18 + 10 + 2*20 + 14
    return {'r': r, 'lines': lines, 'h': h}

specs = [card_spec(r) for r in results]
colw = (W - PAD*2 - 12) / 2
yy = y
for i in range(0, len(specs), 2):
    row = specs[i:i+2]
    rh = max(s['h'] for s in row)
    for j, s in enumerate(row):
        r = s['r']; x = PAD + j*(colw+12)
        rr(x, yy, x+colw, yy+rh, PANEL, BORDER, r=10)
        ix = x + 16; iy = yy + 14
        # top row
        txt(ix, iy+2, r['id'], f_mono, GRAY); ix += tw(r['id'], f_mono) + 8
        ix += badge(ix, iy, r['severity'], SEVC[r['severity']]) + 6
        tw_ = tw(r['team'], f_chip) + 14
        rr(ix, iy, ix+tw_, iy+22, PANEL, BORDER, r=5)
        txt(ix+7, iy+3.5, r['team'], f_chip, GRAY); ix += tw_ + 6
        act = r['action'].split(' + ')[0]
        fg = {'PAGE': RED, 'TICKET': BLUE, 'DROP': GRAY, 'DEDUP': PURPLE, 'LOG': GRAY}[act.split()[0]]
        tint = tuple(int(c*0.16+BG[k]*0.84) for k, c in enumerate(fg))
        ix += badge(ix, iy, act, fg, tint) + 6
        if 'HUMAN REVIEW' in r['action']:
            ix += badge(ix, iy, 'HUMAN REVIEW', AMBER, dashed=True) + 6
        ms = f"{r['ms']}ms"
        txt(x+colw-16-tw(ms, f_mono), iy+3, ms, f_mono, GRAY)
        iy += 30
        txt(x+16, iy, alerts[r['id']]['title'][:70], f_b); iy += 24
        for ln in s['lines']:
            txt(x+16, iy, ln, f_t, GRAY); iy += 18
        iy += 8
        for lab, val, col in [('severity conf', r['sev_conf'], SEVC[r['severity']]),
                              ('duplicate p', r['dup_p'], PURPLE)]:
            txt(x+16, iy+1, lab, f_small, GRAY)
            bx = x+16+96; bw = colw-32-96-44
            rr(bx, iy+4, bx+bw, iy+11, BG, BORDER, r=4)
            fw = max(2, bw*val)
            rr(bx, iy+4, bx+fw, iy+11, col, r=4)
            txt(bx+bw+8, iy+1, f'{val:.2f}', f_mono, GRAY)
            iy += 20
    yy += rh + 12
y = yy + 6

# agreement row
ags = [('13/14', 'actionable'), ('10/14', 'severity'), ('12/14', 'team'), ('13/14', 'duplicate')]
for i, (n, l) in enumerate(ags):
    x = PAD + i*(cw+12)
    rr(x, y, x+cw, y+72, PANEL, BORDER, r=8)
    n_ = n; tx = x + cw/2 - tw(n_, f_bign)/2
    txt(tx, y+10, n_, f_bign, GREEN)
    txt(x + cw/2 - tw(l, f_lab)/2, y+44, l, f_lab, GRAY)
y += 72 + 14
foot = 'Agreement vs author\u2019s labels  ·  every miss carried low confidence and went to human review  ·  8 paged · 4 dropped · 1 deduped'
txt(W/2 - tw(foot, f_lab)/2, y, foot, f_lab, GRAY)
y += 30

img = img.crop((0, 0, W*S, int(y*S)))
img.save(base + '/dashboard.png')
print('saved', img.size)
