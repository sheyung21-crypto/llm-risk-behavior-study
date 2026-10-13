"""Typeset academic figures/tables from pinned aggregates; no statistical estimation."""
from pathlib import Path
import argparse,csv,hashlib,io,json,re,math
from xml.sax.saxutils import escape
from reportlab.graphics.shapes import Drawing,Rect,Line,String,Circle,Polygon,Group
from reportlab.graphics import renderPDF,renderSVG
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color,white,black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from pypdf import PdfReader
import pypdfium2 as pdfium
ROOT=Path(__file__).resolve().parents[1];OUT=None
ALIASES={'outputs/machine_readable_results.json': 'data/aggregate/frozen_results.json', 'outputs/tables/paired_condition_comparisons.csv': 'data/aggregate/paired_condition_comparisons.csv', 'outputs/tables/stage1_h8_item_results.csv': 'data/aggregate/stage1_h8_item_results.csv', 'outputs/tables/k_stability_results.csv': 'data/aggregate/k_stability_results.csv', 'outputs/tables/descriptive_distributions.csv': 'data/aggregate/descriptive_distributions.csv', 'outputs/tables/metric_scale_audit.csv': 'data/aggregate/metric_scale_audit.csv', 'outputs/tables/analysis_ledger.csv': 'data/aggregate/analysis_ledger.csv', 'paper/tables/table_s2_list_schedule.csv': 'data/aggregate/table_s2_list_schedule.csv', 'paper/tables/table_s4_reproducibility_hashes.csv': 'data/aggregate/table_s4_reproducibility_hashes.csv'}
W=16*cm;FONT='JournalSans';READS={};ASSETS=[];DRAW_TEXT=[];PLOT_DATA=[];TABLE_DATA={}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(rel):
    if rel not in ALIASES: raise ValueError('Unlisted input: '+rel)
    public=ALIASES[rel];p=ROOT/public
    if p.is_symlink() or sha(p)!=PINS[public]: raise ValueError('Input integrity failure: '+public)
    READS[public]=sha(p);return p.read_text(encoding='utf-8-sig')
def rows(rel):return list(csv.DictReader(io.StringIO(read(rel))))
def write_csv(path,records):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        if records:
            w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
def txt(d,x,y,s,size=9.5,anchor='start',font=FONT):
    s=str(s).replace('−','-')
    width=pdfmetrics.stringWidth(s,font,size)
    left=x if anchor=='start' else x-width if anchor=='end' else x-width/2
    d.add(String(x,y,s,fontName=font,fontSize=size,fillColor=black,textAnchor=anchor))
    DRAW_TEXT.append({'text':s,'left':left,'right':left+width,'y':y,'size_pt':size})
def ln(d,x1,y1,x2,y2,width=.65,dash=None):
    d.add(Line(x1,y1,x2,y2,strokeColor=black,strokeWidth=width,strokeDashArray=dash))
def rect(d,x,y,w,h,width=.65):
    d.add(Rect(x,y,w,h,fillColor=white,strokeColor=black,strokeWidth=width))
def wrap(s,width,size=9,font=FONT):
    out=[]
    for p in str(s).replace('−','-').split('\n'):
        cur=''
        tokens=re.findall(r'[A-Za-z0-9_./+\-]+|.',p)
        for token in tokens:
            pieces=list(token) if pdfmetrics.stringWidth(token,font,size)>width else [token]
            for ch in pieces:
                if cur and pdfmetrics.stringWidth(cur+ch,font,size)>width:
                    if ch in '，。；：、）】！？' and len(cur)>1:
                        tail=re.search(r'[A-Za-z0-9_./+\-]+$',cur)
                        take=tail.group() if tail and len(tail.group())<len(cur) else cur[-1]
                        out.append(cur[:-len(take)].rstrip());cur=take+ch
                    else:out.append(cur.rstrip());cur=ch.lstrip()
                else:cur+=ch
        out.append(cur.rstrip())
    return out
def para(d,x,y,s,width,size=9,leading=13):
    a=wrap(s,width,size)
    for i,t in enumerate(a):txt(d,x,y-i*leading,t,size)
    return y-len(a)*leading
def box(d,x,y,w,h,s,size=9):
    rect(d,x,y,w,h)
    a=wrap(s,w-12,size);leading=13
    assert len(a)*leading<=h-8,('box overflow',s)
    top=y+h/2+(len(a)-1)*leading/2-size*.35
    for i,t in enumerate(a):txt(d,x+w/2,top-i*leading,t,size,'middle')
def arrow(d,pts,dash=None):
    for a,b in zip(pts,pts[1:]):ln(d,*a,*b,.65,dash)
    (a,b),(x,y)=pts[-2:]
    if abs(x-a)>abs(y-b):
        sign=1 if x>a else -1;coords=[x,y,x-4*sign,y+2,x-4*sign,y-2]
    else:
        sign=1 if y>b else -1;coords=[x,y,x-2,y-4*sign,x+2,y-4*sign]
    d.add(Polygon(coords,fillColor=black,strokeColor=None))
def axis(d,x0,x1,y,lo,hi,ticks,label):
    ln(d,x0,y,x1,y)
    for t in ticks:
        x=x0+(t-lo)/(hi-lo)*(x1-x0)
        ln(d,x,y,x,y-3);txt(d,x,y-15,f'{t:g}',9,'middle')
    if label:txt(d,(x0+x1)/2,y-31,label,9.5,'middle')
def marker(d,x,y,kind='circle',filled=True,size=3):
    if kind=='circle':obj=Circle(x,y,size,fillColor=black if filled else white,strokeColor=black,strokeWidth=.8)
    elif kind=='square':obj=Rect(x-size,y-size,2*size,2*size,fillColor=black if filled else white,strokeColor=black,strokeWidth=.8)
    else:obj=Polygon([x,y+size+1,x+size+1,y-size,x-size-1,y-size],fillColor=black if filled else white,strokeColor=black,strokeWidth=.8)
    d.add(obj)
def ci(d,asset,label,metric,x0,x1,y,lo,hi,value,bounds,kind='circle',filled=True,source='',n=342):
    X=lambda v:x0+(v-lo)/(hi-lo)*(x1-x0)
    a,b=bounds
    ln(d,X(a),y,X(b),y,.8)
    for v in [a,b]:ln(d,X(v),y-3,X(v),y+3,.65)
    marker(d,X(value),y,kind,filled)
    PLOT_DATA.append({'asset':asset,'series':label,'metric':metric,'N':n,'estimate':value,'ci_low':a,'ci_high':b,'axis_min':lo,'axis_max':hi,'x0_pt':x0,'x1_pt':x1,'y_pt':y,'point_x_pt':X(value),'ci_left_pt':X(a),'ci_right_pt':X(b),'source':source})
def caption_asset(name,core,title,note,kind='figure'):
    lines=wrap(note,W,9);extra=38+len(lines)*13
    d=Drawing(W,core.height+extra)
    d.add(Rect(0,0,W,d.height,fillColor=white,strokeColor=None))
    g=Group();g.add(core);g.translate(0,extra);d.add(g)
    txt(d,0,extra-17,title,10.5)
    para(d,0,extra-33,note,W,9,13)
    ASSETS.append({'name':name,'drawing':d,'title':title,'note':note,'kind':kind,'core_offset':extra,'core':core})
    return d
def table(name,title,headers,records,widths,note,numeric=(),font_size=9):
    assert abs(sum(widths)-W)<.01
    header_lines=[wrap(x,w-8,font_size) for x,w in zip(headers,widths)]
    hh=max(map(len,header_lines))*13+12
    line_rows=[[wrap(str(v),w-8,font_size) for v,w in zip(row,widths)] for row in records]
    rh=[max(map(len,row))*13+12 for row in line_rows]
    notes=wrap(note,W,9)
    height=28+hh+sum(rh)+13+len(notes)*13+5
    d=Drawing(W,height);d.add(Rect(0,0,W,height,fillColor=white,strokeColor=None))
    txt(d,0,height-11,title,10.5)
    top=height-27;ln(d,0,top,W,top,1)
    xs=[0]
    for w in widths:xs.append(xs[-1]+w)
    for i,ls in enumerate(header_lines):
        for j,s in enumerate(ls):
            anchor='end' if i in numeric else 'start';xx=xs[i+1]-4 if anchor=='end' else xs[i]+4
            txt(d,xx,top-15-j*13,s,font_size,anchor)
    y=top-hh;ln(d,0,y,W,y,.6)
    for row,h in zip(line_rows,rh):
        for i,ls in enumerate(row):
            for j,s in enumerate(ls):
                anchor='end' if i in numeric else 'start';xx=xs[i+1]-4 if anchor=='end' else xs[i]+4
                txt(d,xx,y-15-j*13,s,font_size,anchor)
        y-=h
    ln(d,0,y,W,y,1);para(d,0,y-16,note,W,9,13)
    ASSETS.append({'name':name,'drawing':d,'title':title,'note':note,'kind':'table','core_offset':0})
    TABLE_DATA[name]={'headers':headers,'rows':records,'title':title,'note':note}
def flow():
    d=Drawing(W,467)
    box(d,0,423,133,40,'原始樣本 409 人\n波昂學生：問卷與實驗')
    box(d,160,423,133,40,'完整案例 382 人\n問卷欄位與行為均完整')
    box(d,320,423,W-320,40,'確認性樣本 342 人')
    arrow(d,[(133,443),(160,443)]);arrow(d,[(293,443),(320,443)])
    txt(d,162,408,'另 40 人分派至技術先導，排除於正式分析',8.5)
    # Direct questionnaire input to each condition and the independent H8 branch.
    arrow(d,[(387,423),(387,389),(153,389),(153,370)])
    txt(d,0,386,'單一真人問卷',9)
    for cx in [49,152,255]:
        if cx!=153:arrow(d,[(153,379),(cx,379),(cx,370)])
    box(d,0,316,98,54,'C1 廣泛問卷\n46 題；主要條件\nK=1',9)
    box(d,103,316,98,54,'C2 嚴格問卷\n移除 R48／R50\n44 題；敏感度',9)
    box(d,206,316,98,54,'C3 近端資訊\n加入 R49\n47 題；診斷',9)
    box(d,326,311,W-326,63,'Stage 1 問卷檢查\n38 題輸入 → 預測 H8\n與保留的 8 題真人答案比較',8.7)
    arrow(d,[(387,389),(390,389),(390,374)])
    for cx in [49,152,255]:arrow(d,[(cx,316),(cx,298),(152,298),(152,283)])
    box(d,0,208,304,75,'兩張風險選擇表（List 1／List 2）\n每張 21 列：彩券或確定點數\n記錄由彩券轉向確定選項的列數\n兩表平均 → 個人分數（1–22）',9)
    box(d,326,208,W-340,75,'K 穩定性診斷\n30 人的 C1 重複 5 次\n評估生成穩定性\n主要分數仍為 K=1',9)
    arrow(d,[(304,234),(326,234)])
    txt(d,306,243,'C1',8.5)
    box(d,0,130,304,54,'盲態計分與 H8、K 診斷封存\n核准後解鎖真人行為結果',9)
    arrow(d,[(152,208),(152,184)])
    arrow(d,[(390,208),(390,194),(276,194),(276,184)])
    arrow(d,[(390,311),(390,299),(W-2,299),(W-2,195),(292,195),(292,184)])
    box(d,326,123,W-326,61,'真人行為 x_risk_av\n原實驗兩表平均轉換列\n僅於解鎖後合併',8.7)
    box(d,0,53,304,53,'同一人的模型分數與真人行為配對\n主要：C1 排序與尺度誤差\n敏感度：C2；任務近端診斷：C3',9)
    arrow(d,[(152,130),(152,106)]);arrow(d,[(326,151),(313,151),(313,79),(304,79)])
    txt(d,0,24,'統計參考另列：嶺迴歸（Ridge）、原研究 R2+R3 量尺組合。',9)
    note='註：資料來自偏好調查模組（Falk 等，2023）。54 個風險問卷欄位及行為結果完整者納入；15 人缺問卷、12 人缺行為。C2／C3 均為 K=1。K 是一份兩表平均分數的重複次數。H8 為八題保留問卷；模型 H8 不回填 C1／C2／C3。R48 為假想中獎後投入彩券的金額，R50 為有收入風險的轉職選擇，R49 為放棄彩券所需的確定金額。彩券為各半機率得 1,000 或 0 點；List 1 的確定點數每列增加 50，List 2 為固定擾動最多 5 點的重建表。全選確定選項記 1，全選彩券記 22；分數越高表示越晚轉向安全選項。圖示為資料關係；實際先完成 C1／C3，再補 Stage 1 與 C2／K。正式結果隔離不表示資料準備從未讀取真人結果。'
    caption_asset('figure_1_study_flow',d,'圖 1　研究設計、樣本與分析關係',note)
def correlation():
    name='figure_2_rank_correspondence';d=Drawing(W,327)
    txt(d,0,310,'A　各條件與真人的排序對應',10);txt(d,W,310,'N=342',9,'end')
    x0,x1=103,277
    ln(d,x0,191,x0,291,.5,[2,2])
    for y,c,shape,fill in [(274,'C1','circle',True),(236,'C2','square',False),(198,'C3','triangle',False)]:
        r=RESULT['conditions'][c];bounds=RESULT['condition_bootstrap_95_ci'][c]['spearman']
        txt(d,0,y-3,c+'　'+{'C1':'主要','C2':'敏感度','C3':'診斷'}[c],9.5)
        ci(d,name,c,'spearman',x0,x1,y,0,.5,r['spearman'],bounds,shape,fill,'outputs/machine_readable_results.json')
        txt(d,W,y-3,f"{r['spearman']:.4f} [{bounds[0]:.4f}, {bounds[1]:.4f}]",8.5,'end')
    axis(d,x0,x1,177,0,.5,[0,.1,.2,.3,.4,.5],'Spearman ρ')
    txt(d,0,125,'B　相對於 C1 的配對相關差異',10)
    zero=x0+.2/.3*(x1-x0);ln(d,zero,47,zero,109,.5,[2,2])
    for y,r,shape in zip([98,62],PAIRED,['square','triangle']):
        c=r['condition'];v=float(r['rho_delta_other_minus_c1']);bounds=[float(r[k]) for k in ['rho_delta_ci_low','rho_delta_ci_high']]
        txt(d,0,y-3,c+' - C1',9.5)
        ci(d,name,c+'-C1','delta_spearman',x0,x1,y,-.2,.1,v,bounds,shape,False,'outputs/tables/paired_condition_comparisons.csv')
        txt(d,W,y-3,f'{v:.4f} [{bounds[0]:.4f}, {bounds[1]:.4f}]',8.5,'end')
    axis(d,x0,x1,41,-.2,.1,[-.2,-.1,0,.1],'Δρ（其他條件 - C1）')
    caption_asset(name,d,'圖 2　個體排序對應與條件差異','註：點為估計值，水平線及方括號為既有 95% 拔靴信賴區間。C1 為唯一主要條件；C2 為敏感度分析，C3 為診斷。B 使用受試者配對相關差異區間，不由 A 的區間重疊推論差異。虛線為零；差異區間包含零不表示條件等效。')
def distrow(d,y,label,r,x0=82,x1=W-7):
    X=lambda v:x0+(v-1)/21*(x1-x0)
    txt(d,0,y-3,label,9.5)
    ln(d,X(r['minimum']),y,X(r['maximum']),y,.7)
    for key in ['minimum','maximum']:ln(d,X(r[key]),y-4,X(r[key]),y+4,.7)
    rect(d,X(r['q25']),y-7,X(r['q75'])-X(r['q25']),14,.7)
    ln(d,X(r['median']),y-7,X(r['median']),y+7,1)
    xx=X(r['mean']);ln(d,xx-2.5,y-2.5,xx+2.5,y+2.5,.8);ln(d,xx-2.5,y+2.5,xx+2.5,y-2.5,.8)
    txt(d,x0,y-21,f"平均 {r['mean']:.4f}；SD {r['sd']:.4f}；中位數 {r['median']:.1f}",8.5)
def scale_figure():
    name='figure_3_scale_and_distribution';d=Drawing(W,290)
    txt(d,0,276,'A　真人與 C1 的分布摘要',10);txt(d,W,276,'N=342',9,'end')
    distrow(d,243,'真人',DESC['human_x_risk_av']);distrow(d,197,'C1（主要）',DESC['C1'])
    axis(d,82,W-7,156,1,22,[1,5,10,15,20,22],'分數：兩表平均轉換列')
    txt(d,0,102,'B　C1 的尺度指標',10)
    specs=[(0,140,'平均偏差（列）','bias',-4,0,[-4,-2,0],0),(156,140,'平均絕對誤差（列）','mae',0,5,[0,2,4,5],0),(312,W-312,'標準差比','sd_ratio',0,1.5,[0,.5,1,1.5],1)]
    for x,w,title,key,lo,hi,ticks,ref in specs:
        txt(d,x+w/2,83,title,9,'middle');a,b=x+10,x+w-10
        xx=a+(ref-lo)/(hi-lo)*(b-a);ln(d,xx,28,xx,68,.5,[2,2])
        v=RESULT['conditions']['C1'][key];bounds=RESULT['condition_bootstrap_95_ci']['C1'][key]
        ci(d,name,'C1',key,a,b,52,lo,hi,v,bounds,source='outputs/machine_readable_results.json')
        axis(d,a,b,28,lo,hi,ticks,'')
    caption_asset(name,d,'圖 3　C1 的分布位置與尺度誤差','註：A 的端點為最小／最大值，框為第 25–75 百分位數，框內直線為中位數，叉號為平均數；端點不是 1.5 倍四分位距鬚線，亦未另判定離群值。B 的點與線為既有估計值及 95% CI。平均偏差=模型-真人，平均絕對誤差為逐人絕對差異的平均，單位均為列；標準差比=模型 SD／真人 SD。虛線依序為 0、0、1。分布摘要不顯示完整密度形狀。')
def h8():
    name='figure_s1_h8';d=Drawing(W,350);txt(d,0,335,'保留題目',9.5);txt(d,W,335,'N=342',9,'end')
    x0,x1=90,278
    ln(d,x0,71,x0,319,.5,[2,2])
    for y,r in zip(range(305,80,-30),H8):
        v=float(r['spearman']);bounds=[float(r[k]) for k in ['spearman_ci_low','spearman_ci_high']]
        txt(d,0,y-3,r['R_item'],9.5)
        ci(d,name,r['R_item'],'spearman',x0,x1,y,0,1,v,bounds,source='outputs/tables/stage1_h8_item_results.csv')
        txt(d,W,y-3,f'{v:.4f} [{bounds[0]:.4f}, {bounds[1]:.4f}]',8.5,'end')
    axis(d,x0,x1,71,0,1,[0,.2,.4,.6,.8,1],'逐題 Spearman ρ')
    txt(d,0,12,'逐題相關平均：Stage 1 0.5308；Benchmark-0 0.3647。',9)
    caption_asset(name,d,'圖 S1　問卷條件化的保留題檢查','註：H8 是八題保留問卷；模型依其餘 38 題預測這八題答案。点與線為逐題 Spearman 相關及既有 95% CI，並非同時信賴區間。Benchmark-0 是其餘 34 題方向對齊後平均的 0–10 分算術參考，不是另一個 LLM。下方比較同一聚合層次的逐題相關平均，未畫未提供的平均值區間或新增差異檢定；它與真人八題平均的相關 0.6897 屬不同聚合層次。'.replace('点','點'))
def kfig():
    name='figure_s2_stability';d=Drawing(W,201);txt(d,0,185,'固定設定下的重複測量',10);txt(d,W,185,'N=30',9,'end')
    x0,x1=146,281
    for y,r in zip([148,103,66],K):
        k=int(r['K']);v=float(r['reliability']);bounds=[float(r['ci_low']),float(r['ci_high'])]
        label='單次 ICC(1,1)' if k==1 else f'平均 {k} 次（換算）'
        txt(d,0,y-3,label,9.5)
        ci(d,name,str(k),'reliability',x0,x1,y,0,1,v,bounds,'circle',k==1,'outputs/tables/k_stability_results.csv',30)
        txt(d,W,y-3,f'{v:.4f} [{bounds[0]:.4f}, {bounds[1]:.4f}]',8.5,'end')
    axis(d,x0,x1,35,0,1,[0,.25,.5,.75,1],'信度')
    caption_asset(name,d,'圖 S2　單次生成穩定性與平均信度換算','註：30 人各有五次兩表測量；主要 K 永久固定為 1。實心點為單次組內相關係數 ICC(1,1)，空心點為同一變異成分換算的平均信度；線為既有 95% CI。q_m=mq/[1+(m-1)q]，q 為單次 ICC，m 為平均次數，q_m 為預期平均信度，回答多次平均後的穩定性。換算假設相同變異結構、可交換重複及誤差不相關；不是三個獨立效度實驗。')
def dist_supp():
    d=Drawing(W,265);txt(d,W,248,'各列 N=342',9,'end')
    for y,key,label in [(224,'human_x_risk_av','真人'),(173,'C1','C1 主要'),(122,'C2','C2 敏感度'),(71,'C3','C3 診斷')]:distrow(d,y,label,DESC[key])
    axis(d,82,W-7,31,1,22,[1,5,10,15,20,22],'分數：兩表平均轉換列')
    caption_asset('figure_s3_condition_distributions',d,'圖 S3　真人與三種問卷條件的分布摘要','註：所有列使用相同尺度；端點為最小／最大值，框為第 25–75 百分位數，直線為中位數，叉號為平均數。只呈現既有摘要，未估計密度或另判定離群值。C3 均值接近真人，亦伴隨較大分散程度；不能僅據平均接近宣稱校準改善。')
def main_tables():
    table('table_1_design','表 1　問卷資訊條件與分析角色',['條件','問卷輸入','分析角色','題數'],
    [['C1：廣泛問卷','核准的風險問卷回答，含 H8、R48、R50','主要條件','46'],
     ['C2：嚴格問卷','C1 移除 R48、R50','敏感度分析','44'],
     ['C3：近端資訊','C1 加入 R49','診斷分析','47']],
    [85,220,100,W-405],'註：三條件均為 342 人、K=1；每人各答兩張表，每個條件共 684 次作答。H8 為八題保留問卷；它們僅在獨立 Stage 1 中不提供給模型，在 C1／C2／C3 中提供真人答案。R48 為假想中獎後投入彩券金額；R50 為有收入風險的轉職選擇；R49 為放棄彩券所需的確定金額。',(3,))
    records=[]
    for c in ['C1','C2','C3']:
        r=RESULT['conditions'][c];a,b=RESULT['condition_bootstrap_95_ci'][c]['spearman']
        records.append([c+' '+{'C1':'主要','C2':'敏感度','C3':'診斷'}[c],f"{r['spearman']:.4f}\n[{a:.4f}, {b:.4f}]",f"{r['pearson']:.4f}",f"{r['mae']:.4f}",f"{r['bias']:.4f}",f"{r['sd_ratio']:.4f}"])
    table('table_2_results','表 2　主要、敏感度與診斷條件的結果',['條件','Spearman ρ\n[95% CI]','Pearson r','MAE\n（列）','平均偏差\n（列）','標準差比'],records,
    [64,119,63,62,65,W-373],'註：各列 N=342。CI 為既有 95% 拔靴信賴區間；C1 為唯一主要條件。MAE 為平均絕對誤差；平均偏差=模型-真人；標準差比=模型 SD／真人 SD。所有分數為兩表平均轉換列（1–22）。相關反映排序或線性對應，不能取代數值校準。',(1,2,3,4,5))
def supplemental_tables():
    r=RESULT['ridge_reference'];r2=RESULT['published_r2_r3_context'];med=RESULT['goal8_5_reconciliation']['post_hoc_descriptive_scale_reference']['constant_median_mae']
    table('table_s1_references','表 S1　統計參考的資訊條件與尺度',['方法／角色','資訊及資料來源','結果'],
    [['Ridge\n監督式參考','巢狀交叉驗證；訓練折使用真人結果，受評估者採折外預測。',f"ρ = {r['spearman']:.4f}\nMAE = {r['mae']:.4f} 列\nR² = {r['r_squared_oof']:.4f}"],
     ['R2+R3\n量尺背景','既有選題及權重來自同一原始研究；非獨立外部測試。',f"ρ = {r2['spearman']:.4f}\nMAE = {r2['mae_standardized_outcome']:.4f}（標準化）\nR² = {r2['r_squared_standardized_outcome']:.4f}"],
     ['常數中位數\n事後描述','使用同樣本真人中位數；無個體排序能力及折外驗證。',f"MAE = {med:.4f} 列\nρ 不適用"],
     ['真人重測\n背景參考','另一份 44 人資料；測量間隔與樣本不同。','Pearson r = 0.5890']],
    [82,199,W-281],'註：Ridge 為嶺迴歸；R2 為假想彩券與確定金額選擇，R3 為一般風險意願，兩題組合沿用原標準化與權重。ρ 為 Spearman 相關，真人重測則為 Pearson r。前三列 N=342。R2+R3 的標準化 MAE 不與「列」直接相比。不同資訊條件不構成公平排名，未補造 CI 或模型差異檢定。')
    rec=[]
    for r in H8:
        rec.append([r['R_item'],f"{float(r['spearman']):.4f}\n[{float(r['spearman_ci_low']):.4f}, {float(r['spearman_ci_high']):.4f}]",f"{float(r['pearson']):.4f}",f"{float(r['holm_p']):.4f}",f"{float(r['mae']):.4f}",f"{float(r['bias']):.4f}",f"{float(r['sd_ratio']):.4f}"])
    table('table_s2_h8','表 S2　H8 逐題對應與誤差',['題號','Spearman ρ\n[95% CI]','Pearson r','Holm p','MAE\n（分）','平均偏差\n（分）','標準差比'],rec,
    [32,116,61,55,55,63,W-382],'註：各題 N=342；真人及模型答案均為 0–10 分。CI 為逐題 95% 拔靴區間，非同時區間；p 為原分析 Holm 調整結果。MAE 為平均絕對誤差，平均偏差=模型-真人，標準差比=模型 SD／真人 SD。表中統一顯示四位小數，完整精度及其餘摘要保留於隨附 CSV。',(1,2,3,4,5,6),8.5)
    schedule=rows('paper/tables/table_s2_list_schedule.csv')
    table('table_s3_list_schedule','表 S3　兩張風險選擇表的確定點數',['列','List 1（點）','List 2（點）','List 2 - List 1（點）'],[[r['row'],r['list_1_safe_points'],r['list_2_safe_points'],r['list_2_delta']] for r in schedule],
    [40,130,130,W-300],'註：每列均在彩券與確定點數間擇一。彩券為各半機率得 1,000 或 0 點；List 1 的確定點數由 0 至 1,000、每列增加 50。List 2 為研究者依事前規則固定擾動最多 5 點的重建表。記錄由彩券轉向確定選項的列數，兩表平均形成個人分數；全選確定選項記 1，全選彩券記 22。',(0,1,2,3))
    role_rows=[
    ['H8 問卷檢查','診斷／推論','342','八個逐題相關的平均；向量置換'],
    ['Benchmark-0','診斷／描述','342','逐題相關平均及真人 H8 平均相關'],
    ['K 穩定性','診斷／描述','30','單次 ICC；平均信度換算'],
    ['C1 排序','主要／推論','342','Spearman ρ；95% CI 與置換'],
    ['C1 尺度','次要／描述','342','Pearson r、MAE、偏差、SD 比'],
    ['C2 嚴格條件','敏感度／推論','342','條件指標及相對 C1 的配對差異'],
    ['C3 近端條件','診斷／推論','342','條件指標及相對 C1 的配對差異'],
    ['Ridge 折外預測','背景／描述','342','相關、列尺度 MAE、R²'],
    ['R2+R3','背景／描述','342','相關、標準化尺度 MAE、R²'],
    ['真人重測','背景／描述','44','Pearson r'],
    ['常數中位數','事後／描述','342','列尺度 MAE；相對誤差比'],
    ['以真人 SD 標準化 MAE','事後／描述','342','MAE／真人 SD']]
    table('table_s4_analysis_roles','表 S4　分析角色、樣本數與尺度',['分析','角色／性質','N','指標與解讀'],role_rows,[111,98,35,W-244],
    '註：C1 是唯一主要行為條件。C1／C2／C3 與真人結果使用 1–22 的轉換列尺度；Ridge 預測同一原始尺度但本身不受此範圍限制。R2+R3 的 MAE 以同樣本標準化結果計算，不能與列尺度 MAE 直接比較。最後兩列為結果解鎖後的尺度描述，其餘角色與設計在正式結果解鎖前固定。完整尺度對照保留於隨附 CSV。',(2,),8.5)
    hashes=rows('paper/tables/table_s4_reproducibility_hashes.csv')
    labels=['PSM 主樣本','真人重測樣本','原研究線上附錄','原資料說明','行為模擬背景報告','C1／C3 工作配置','List 1 排程','List 2 重建排程','C1 提示範本']
    records=[[label,Path(r['artifact']).name,r['sha256'][:32]+'\n'+r['sha256'][32:]] for label,r in zip(labels,hashes)]
    table('table_s5_material_identifiers','表 S5　研究材料識別與版本查核',['材料','檔案識別','SHA-256'],records,[88,151,W-239],
    '註：SHA-256 是檔案內容的完整雜湊識別碼，每列兩行需連接後使用；不是截短值。此表沿用封存來源紀錄，並未重新讀取逐人資料。主樣本與真人重測資料需受限取用；列入此表不表示取得第三方材料再散布授權。完整相對路徑與來源紀錄保留於隨附 CSV。',(),8.5)

def export():
    (OUT/'figures').mkdir(parents=True,exist_ok=True);(OUT/'tables').mkdir(exist_ok=True);(OUT/'data').mkdir(exist_ok=True);(OUT/'editable').mkdir(exist_ok=True)
    flow();main_tables();correlation();scale_figure();h8();kfig();dist_supp();supplemental_tables()
    pdfpath=OUT/'figures_tables.pdf'
    c=canvas.Canvas(str(pdfpath),pagesize=A4,invariant=1);c.setTitle('論文圖表審閱：學術黑白版');c.setAuthor('Research project')
    outline=[];svgtexts={}
    for i,a in enumerate(ASSETS,1):
        name=a['name'];d=a['drawing'];kind=a['kind'];folder=OUT/('figures' if kind=='figure' else 'tables')
        assert d.height<=A4[1]-5*cm,(name,d.height,'exceeds A4 content height')
        path=folder/(name+'.svg')
        renderSVG.drawToFile(d,str(path))
        svg=path.read_text(encoding='utf-8').replace('font-family: JournalSans','font-family: Microsoft JhengHei, sans-serif')
        svg=svg.replace('<title>...</title>','<title>'+escape(a['title'])+'</title>').replace('<desc>...</desc>','<desc>'+escape(a['note'])+'</desc>')
        path.write_text(svg,encoding='utf-8')
        if kind=='figure':
            corepath=OUT/'editable'/(name+'.svg')
            renderSVG.drawToFile(a['core'],str(corepath))
            coresvg=corepath.read_text(encoding='utf-8').replace('font-family: JournalSans','font-family: Microsoft JhengHei, sans-serif')
            coresvg=coresvg.replace('<title>...</title>','<title>'+escape(a['title'])+'</title>').replace('<desc>...</desc>','<desc>'+escape(a['note'])+'</desc>')
            corepath.write_text(coresvg,encoding='utf-8')
        doc=pdfium.PdfDocument(renderPDF.drawToString(d));doc[0].render(scale=300/72).to_pil().save(folder/(name+'.png'))
        # Draw at native point dimensions: no reduction of labels on the 16 cm page width.
        renderPDF.draw(d,c,2.5*cm,A4[1]-2.5*cm-d.height)
        c.setFont(FONT,9);c.drawCentredString(A4[0]/2,1.3*cm,f'{i} / {len(ASSETS)}');c.showPage()
        outline.append({'name':name,'kind':kind,'title':a['title'],'note':a['note'],'page':i,'width_cm':16,'height_pt':d.height,'core_offset_pt':a['core_offset'],'svg':path.relative_to(OUT).as_posix(),'png':(folder/(name+'.png')).relative_to(OUT).as_posix()})
        if kind=='table':
            t=TABLE_DATA[name];records=[dict(zip(t['headers'],r)) for r in t['rows']]
            write_csv(folder/(name+'.csv'),records)
            md=t['title']+'\n\n|'+'|'.join(h.replace('\n',' ') for h in t['headers'])+'|\n|'+'|'.join('---' for _ in t['headers'])+'|\n'
            md+=''.join('|'+'|'.join(str(v).replace('\n','<br>') for v in row)+'|\n' for row in t['rows'])
            md+='\n'+t['note']+'\n';(folder/(name+'.md')).write_text(md,encoding='utf-8')
    c.save()
    assert len(PdfReader(pdfpath).pages)==13
    write_csv(OUT/'data/plot_points.csv',PLOT_DATA)
    for rel in ['outputs/tables/stage1_h8_item_results.csv','outputs/tables/descriptive_distributions.csv','outputs/tables/k_stability_results.csv','outputs/tables/paired_condition_comparisons.csv','outputs/tables/metric_scale_audit.csv','outputs/tables/analysis_ledger.csv','paper/tables/table_s4_reproducibility_hashes.csv','paper/tables/table_s2_list_schedule.csv']:
        # Re-encode as UTF-8 with BOM solely for spreadsheet interoperability; preserve all cells.
        write_csv(OUT/'data'/Path(rel).name,rows(rel))
    (OUT/'data/frozen_results.json').write_text(json.dumps(RESULT,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (OUT/'asset_manifest.json').write_text(json.dumps(outline,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    captions='# 圖名、表名與註解\n\n'
    for a in outline:captions+='## '+a['title']+'\n\n'+a['note']+'\n\n'
    (OUT/'captions.md').write_text(captions,encoding='utf-8')
    # Measure text boxes before nested coordinate transforms; manual review checks full page composition.
    missing=sorted({ch for r in DRAW_TEXT for ch in r['text'] if ord(ch) not in pdfmetrics.getFont(FONT).face.charToGlyph})
    bad=[r for r in DRAW_TEXT if r['left']<-.2 or r['right']>W+.2 or r['y']<-.2]
    assert not missing,('unsupported glyphs',missing)
    assert not bad,('text exceeds width',bad)
    verification={'status':'PASS','figures':6,'tables':7,'pages':13,'inputs':READS,'font_sha256':sha(ARGS.font),
      'font_family':'Microsoft JhengHei','width_cm':16,'font_sizes_pt':sorted({r['size_pt'] for r in DRAW_TEXT}),
      'white_background':True,'black_white_only':True,'numeric_decimals':4,'paragraphs_rewritten':0,
      'statistical_analyses_run':0,'participant_level_inputs_read':0,'unsupported_glyphs':missing,
      'text_bounds_errors':bad,'text_count':len(DRAW_TEXT),'plotted_interval_count':len(PLOT_DATA),
      'visual_review':'Reference PDF reviewed; replacement fonts require visual review.'}
    (OUT/'build_verification.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'PASS','pages':13,'assets':len(ASSETS),'min_font_size':verification['font_sizes_pt'][0],'intervals':len(PLOT_DATA)}))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description="Recreate 6 figures and 7 tables from bundled frozen aggregates; never fit statistics.")
    parser.add_argument('--font',type=Path,required=True,help="Locally installed CJK TrueType font; no font is redistributed.")
    parser.add_argument('--output',type=Path,required=True,help="New output directory outside this package.")
    ARGS=parser.parse_args()
    if not ARGS.font.is_file():parser.error("Font does not exist.")
    candidate=ARGS.output.absolute()
    if candidate.exists() or candidate.is_symlink():parser.error("Output already exists; choose a new directory.")
    for parent in [candidate,*candidate.parents]:
        if parent.is_symlink() or (hasattr(parent,'is_junction') and parent.is_junction()):
            parser.error("Linked output paths are not supported.")
    OUT=candidate.resolve()
    if OUT==ROOT or ROOT in OUT.parents or OUT in ROOT.parents:
        parser.error("Output must be outside this package and must not contain it.")
    PINS=json.loads((ROOT/'provenance/plot_inputs.json').read_text(encoding='utf-8'))
    pdfmetrics.registerFont(TTFont(FONT,str(ARGS.font),subfontIndex=0))
    RESULT=json.loads(read('outputs/machine_readable_results.json'))
    PAIRED=rows('outputs/tables/paired_condition_comparisons.csv')
    H8=rows('outputs/tables/stage1_h8_item_results.csv');K=rows('outputs/tables/k_stability_results.csv')
    DESC={r['measure']:{k:float(v) for k,v in r.items() if k!='measure'} for r in rows('outputs/tables/descriptive_distributions.csv')}
    for alias in ALIASES:read(alias)
    OUT.mkdir(parents=True,exist_ok=False)
    export()
