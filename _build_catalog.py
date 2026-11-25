import json,re,hashlib
from pathlib import Path
from collections import defaultdict
CAT=json.loads(Path('/data/workspace/_catalog.json').read_text(encoding='utf-8'))
P=json.loads(Path('/data/workspace/_probe_all.json').read_text(encoding='utf-8'))
STATE={r['url']:r for r in P}
# 清单章节 → 工程 31 类（0 表示只登记不挂活跃端点）
MAP={1:23,2:2,3:1,4:3,5:4,6:6,7:11,8:12,9:7,10:7,11:14,12:13,13:17,14:13,
     15:13,16:14,17:8,18:21,19:9,20:7,21:10,22:11,23:17,24:12,25:15,26:20,
     27:16,28:25,29:27,30:28,31:30}
BOARDS={r['sec']:r['board'] for r in CAT['rows']}

def short_name(row):
    n=row['name']
    n=re.split(r'\s*/\s*',n)[0].strip()
    n=re.sub(r'[（(].*?[)）]','',n).strip()
    return n[:18] or '未命名渠道'

# ① 完整清单结构化入包：691 行 / 503 URL / 31 章 / 探测状态 / 级别
entries=[]
for i,r in enumerate(CAT['rows'],1):
    us=[]
    for u in r['urls']:
        st=STATE.get(u,{})
        us.append(dict(url=u,code=st.get('code',-1),reachable=st.get('code')==200,
                       links=st.get('links',0),ct=st.get('ct','')))
    entries.append(dict(id=i,sec=r['sec'],board=BOARDS[r['sec']],name=r['name'],
                        level=r['level'],cover=r['cover'],urls=us,
                        mapped_group=MAP[r['sec']]))
catalog=dict(version='1.0',source='资讯渠道清单.md',
             sha256=hashlib.sha256(Path('/data/inputs/资讯渠道清单.md').read_bytes()).hexdigest(),
             sections=len(BOARDS),channels=len(entries),
             unique_urls=len(CAT['urls']),
             reachable_urls=sum(1 for r in P if r['code']==200),
             blocked_urls=sum(1 for r in P if r['code'] in (403,412)),
             note=('全部渠道逐条登记；可达入口按 31 类接入采集台账，'
                   '403/412 入口只在口径表逐类申报缺口，不挂活跃端点以免拖慢整批'),
             entries=entries)
Path('data/channel_catalog.json').write_text(
    json.dumps(catalog,ensure_ascii=False,indent=1)+'\n',encoding='utf-8')
print('清单入包:',catalog['channels'],'行 /',catalog['unique_urls'],'URL / 可达',catalog['reachable_urls'])

# ② 可达入口按 31 类接入 sources.json
S=json.loads(Path('sources.json').read_text(encoding='utf-8'))
G={g['id']:g for g in S['groups']}
existing={e.get('url','') for g in S['groups'] for e in g['endpoints']}
# 已知停更 / 省略号 / 非新闻数据源，不挂活跃端点
SKIP=re.compile(r'people\.com\.cn/rss|news_politics\.xml|news_world\.xml|\.\.\.|weixin\.qq\.com|weibo\.com/|'
                r'news\.google\.com/rss|rsshub\.app|docs\.rsshub',re.I)
added=defaultdict(int); skipped=defaultdict(int)
for r in CAT['rows']:
    gid=MAP[r['sec']]
    for u in r['urls']:
        st=STATE.get(u,{})
        if st.get('code')!=200 or u in existing or SKIP.search(u):
            skipped['不可达/已挂/排除']+=1; continue
        links=st.get('links',0); xml=st.get('xml',False)
        # 只挂能抽到链接的 HTML 栏目页；JS 壳(链接<5)只登记不挂
        if links<5 and not xml:
            skipped['JS壳/无链接']+=1; continue
        name=short_name(r)
        ep=dict(name=name,url=u,type='html',level=r['level'],
                catalog_sec=r['sec'],catalog_board=BOARDS[r['sec']],
                max_articles=6)
        G[gid]['endpoints'].append(ep)
        existing.add(u); added[gid]+=1
S['version']='3.0'
S['scope']=('资讯全渠道采集说明 §2：31 类归并为 8 个板块；本文件是采集端点真值源。'
            'v3.0：接入《资讯渠道清单》691 行 / 503 URL，其中可达入口按 31 类接入；'
            '完整清单与逐 URL 探测状态见 data/channel_catalog.json')
Path('sources.json').write_text(json.dumps(S,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
total=sum(len(g['endpoints']) for g in S['groups'])
print('台账端点:',total,'(新增',sum(added.values()),')')
print('逐类新增:',dict(sorted(added.items())))
print('跳过:',dict(skipped))
print('\n各章登记/可达/接入:')
for s in range(1,32):
    us={u for r in CAT['rows'] if r['sec']==s for u in r['urls']}
    okr=sum(1 for u in us if STATE.get(u,{}).get('code')==200)
    print(f'  §{s:<2}{BOARDS[s][:14]:16s} 登记{len(us):>3} 可达{okr:>3} 接入§{MAP[s]:<2} 新增{added[MAP[s]]:>2}')
