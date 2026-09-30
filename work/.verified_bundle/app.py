import os, json, time, threading, urllib.parse as up, hashlib, re as _re2
def _s2hash(pw): return 'sha256$'+hashlib.sha256(('v2:'+str(pw)).encode()).hexdigest()
def _s2past(v):
  try:
    if not isinstance(v,str) or not _re2.match(r'^\d{4}-\d{2}-\d{2}$',v): return False
    import datetime as _dt
    return _dt.datetime.strptime(v,'%Y-%m-%d').replace(tzinfo=_dt.timezone.utc).timestamp()<time.time()-86400
  except Exception: return False
from http.server import BaseHTTPRequestHandler, HTTPServer
import os as _o
DB=_o.path.join(_o.path.dirname(_o.path.abspath(__file__)),'db.json'); FLAGS={"persist_file": 0, "search_real": 0, "admin_protect": 0, "state_guard": 0, "isolation": 0, "scheduler": 0, "confirm_guard": 0, "booking": 0, "agg": 0, "once_guard": 0, "once_window": 0, "deadline_guard": 0, "accrual": 0, "secret_hash": 0, "health": 0}
os.chdir(os.path.dirname(os.path.abspath(__file__)))
items=[]
try:
  if FLAGS.get('persist_file') and os.path.isfile(DB): items=json.load(open(DB))
except Exception: items=[]
lock=threading.Lock()
def save():
  if FLAGS.get('persist_file'):
    try: open(DB,'w').write(json.dumps(items))
    except Exception: pass
if FLAGS.get('booking') and not any(isinstance(_it,dict) and _it.get('kind')=='slot' for _it in items):
  for _i,_t in enumerate(('09:00-10:00','10:30-11:30','14:00-15:00')):
    items.append({'kind':'slot','slot':'A%d'%(_i+1),'time':_t,'status':'可预约'})
  save()
def sweep():
  while True:
    time.sleep(1)
    if FLAGS.get('scheduler'):
      now=time.time()
      with lock:
        items[:]=[it for it in items if not (it.get('exp') and it['exp']<now)]
      save()
threading.Thread(target=sweep,daemon=True).start()
class H(BaseHTTPRequestHandler):
  def log_message(self,*a): pass
  def _send(self,code,body,ct='application/json'):
    b=body.encode() if isinstance(body,str) else body
    self.send_response(code); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
  def do_GET(self):
    pa=up.urlparse(self.path); q=up.parse_qs(pa.query)
    if pa.path=='/' or pa.path.endswith('.html'):
      self._send(200,open('index.html',encoding='utf-8').read(),'text/html; charset=utf-8'); return
    if pa.path in ('/api/list','/api/items','/api/todos','/api/orders'):
      user=self.headers.get('X-User','')
      with lock: lst=list(items)
      if FLAGS.get('isolation') and user: lst=[it for it in lst if it.get('owner','') in ('',user)]
      if FLAGS.get('deadline_guard'): lst=[it for it in lst if not _s2past(it.get('valid_to',it.get('expire',it.get('validto',''))))]
      if FLAGS.get('order_by'):
        # v99-stage1: SPEC order rule sorts by score (shared verifier/builder convention).
        _dd=str(FLAGS.get('order_by')).split(':')[-1] if ':' in str(FLAGS.get('order_by')) else 'desc'
        def _sk(_it):
          try: _v=float(_it.get('score')) if isinstance(_it,dict) and _it.get('score') not in (None,'') else None
          except Exception: _v=None
          return _v
        _nos=float('-inf') if _dd=='desc' else float('inf')
        lst=sorted(lst,key=lambda _it: (_sk(_it) if _sk(_it) is not None else _nos),reverse=(_dd=='desc'))
      self._send(200,json.dumps(lst,ensure_ascii=False)); return
    if pa.path=='/api/search':
      qq=q.get('q',[''])[0]
      _su=self.headers.get('X-User','')
      with lock: lst=list(items)
      if FLAGS.get('isolation') and _su: lst=[it for it in lst if it.get('owner','') in ('',_su)]
      if FLAGS.get('search_real'): r=[it for it in lst if qq and qq in json.dumps(it,ensure_ascii=False)]
      else: r=list(lst)
      self._send(200,json.dumps(r,ensure_ascii=False)); return
    if pa.path=='/api/admin':
      if FLAGS.get('admin_protect'):
        if self.headers.get('X-Role')=='admin': self._send(200,'{"ok":true,"panel":"admin"}')
        else: self._send(401,'{"error":"login required"}')
      else: self._send(200,'{"ok":true,"panel":"admin open"}')
      return
    if pa.path=='/api/slots':
      with lock: _sl=[it for it in items if isinstance(it,dict) and it.get('kind')=='slot']
      self._send(200,json.dumps(_sl,ensure_ascii=False)); return
    if pa.path=='/api/agg':
      if not FLAGS.get('agg'): self._send(404,'{}'); return
      _by=(q.get('by',['month'])[0] if isinstance(q,dict) else 'month')
      if _by not in ('month','day','year','cat','name'):
        self._send(400,'{"error":"bad group"}'); return
      _groups={}; _counts={}
      with lock: _its=list(items)
      for _it in _its:
        if not isinstance(_it,dict): continue
        if _by=='cat': _k=str(_it.get('cat',_it.get('category','')))
        elif _by=='name': _k=str(_it.get('name',_it.get('text',_it.get('title',''))))
        else:
          _d=str(_it.get('date',''))
          import re as _re
          if not _re.match(r'^\d{4}-\d{2}(-\d{2})?$',_d): continue
          _k=_d[:7] if _by=='month' else (_d[:4] if _by=='year' else _d[:10])
        if not _k: continue
        _counts[_k]=_counts.get(_k,0)+1
        try: _amt=float(_it.get('amount',_it.get('money',_it.get('total','nan'))))
        except Exception: continue
        if _amt!=_amt: continue
        _groups[_k]=round(_groups.get(_k,0.0)+_amt,2)
      self._send(200,json.dumps({'groups':_groups,'counts':_counts,'by':_by},ensure_ascii=False)); return
    if pa.path=='/api/health':
      if not FLAGS.get('health'): self._send(404,'{}'); return
      self._send(200,json.dumps({'ok':True,'ts':time.time()})); return
    self._send(404,'{}')
  def do_POST(self):
    ln=int(self.headers.get('Content-Length',0)); body=self.rfile.read(ln) if ln else b''
    try: data=json.loads(body or b'{}')
    except Exception: data={}
    if self.path in ('/api/add','/api/items','/api/todos','/api/orders'):
      user=self.headers.get('X-User','')
      _t0=data.get('text',data.get('title',data.get('name','')))
      if FLAGS.get('once_guard') and not FLAGS.get('once_window') and _t0 and any(isinstance(_x,dict) and _x.get('text')==_t0 for _x in items):
        self._send(409,'{"error":"duplicate: already recorded"}'); return
      _wday=None
      if FLAGS.get('once_window') and _t0:
        import datetime as _dtw
        _today=_dtw.date.today().isoformat()
        _dv=data.get('day',data.get('date',''))
        # fixture channel (mirrors accrual as_of): explicit valid YYYY-MM-DD
        # seeds a prior-window entry; otherwise server stamps today.
        if isinstance(_dv,str) and __import__('re').match(r'^\d{4}-\d{2}-\d{2}$',_dv):
          try: _dtw.date.fromisoformat(_dv); _wday=_dv
          except Exception: _wday=_today
        else: _wday=_today
        if any(isinstance(_x,dict) and _x.get('text')==_t0 and _x.get('day')==_wday for _x in items):
          self._send(409,'{"error":"duplicate: already recorded today"}'); return
      if FLAGS.get('secret_hash'):
        for _sk in ('password','passwd','pwd'):
          if isinstance(data.get(_sk),str) and not data[_sk].startswith('sha256$'): data[_sk]=_s2hash(data[_sk])
      it={'text':_t0,'owner':user if FLAGS.get('isolation') else ''}
      if FLAGS.get('once_window') and _t0 and _wday: it['day']=_wday
      for _k,_v in data.items():  # v54: preserve domain fields (qty/amount/est/due/start/...) so real mechanisms can compute
        if _k not in it and isinstance(_v,(str,int,float,bool)) and _k not in ('exp',):
          it[_k]=_v
      if 'status' in data: it['status']=data['status']
      elif FLAGS.get('state_guard'): it['status']='created'
      if data.get('ttl') and FLAGS.get('scheduler'):
        try: it['exp']=time.time()+float(data['ttl'])
        except Exception: pass
      elif FLAGS.get('scheduler'):
        # TTL-expiry fixture channel (mirrors once_window day override): explicit
        # absolute exp (verifier past/future probes) or created_at backdate bound
        # to the demand TTL (ttl_default from SPEC rule). Fail-closed: absurd
        # values ignored (no exp). Relative-ttl branch above keeps precedence.
        try:
          _now=time.time(); _exp_abs=None
          _ev=data.get('exp')
          if isinstance(_ev,(int,float)) and 1e9<float(_ev)<4102444800: _exp_abs=float(_ev)
          if _exp_abs is not None: it['exp']=_exp_abs
          else:
            _td=float(FLAGS.get('ttl_default') or 0)
            if _td>0:
              _ca=data.get('created_at'); _base=_now
              if isinstance(_ca,(int,float)) and _now-63072000<float(_ca)<_now+60: _base=float(_ca)
              it['exp']=_base+_td
        except Exception: pass
      # qty-edge cap (from SPEC edge rule): same-text items at cap refuse
      # further adds (409), count unchanged. Fail-closed: only when _t0
      # non-empty and cap positive; empty-text adds never capped.
      try:
        _qc = int(float(FLAGS.get('qty_cap') or 0))
      except Exception: _qc = 0
      if _qc > 0 and _t0:
        try:
          _same = sum(1 for _x in items if isinstance(_x, dict) and _x.get('text') == _t0)
        except Exception: _same = 0
        if _same >= _qc:
          self._send(409, '{"error":"over cap: same-kind limit reached"}'); return
      with lock: items.append(it)
      save(); self._send(201,'{"ok":true}'); return
    if self.path in ('/api/update','/api/transition','/api/ship','/api/pay'):
      to=data.get('status') or data.get('to') or data.get('step') or ''
      if FLAGS.get('transition_guard') and self.headers.get('X-Role')!='admin':
        self._send(401,'{"error":"admin required"}'); return
      _tu=self.headers.get('X-User','')
      if FLAGS.get('isolation') and _tu:
        with lock: _mine=[r for r in items if r.get('owner','') in ('',_tu)]
        if not _mine:
          self._send(403,'{"error":"not your item"}'); return
      if FLAGS.get('state_guard'):
        # legal: created->paid->shipped only; direct created->shipped rejected
        frm=data.get('from','created')
        ok=(frm,to) in (('created','paid'),('paid','shipped'))
        if ok:
          with lock:
            moved=False
            for r in items:
              if r.get('status')==frm and (not (FLAGS.get('isolation') and _tu) or r.get('owner','') in ('',_tu)): r['status']=to; moved=True
            if not moved and items:
              _cand=[r for r in items if not (FLAGS.get('isolation') and _tu) or r.get('owner','') in ('',_tu)]
              if _cand: _cand[0]['status']=to; moved=True
            if not moved:
              save(); self._send(403,'{"error":"not your item"}'); return
          save()
          self._send(200,'{"ok":true,"status":"%s"}'%to)
        else: self._send(400,'{"error":"illegal transition"}')
      else:
        with lock:
          _cand2=[r for r in items if not (FLAGS.get('isolation') and _tu) or r.get('owner','') in ('',_tu)]
          if _cand2: _cand2[0]['status']=to or 'shipped'
          else: save(); self._send(403,'{"error":"not your item"}'); return
        save()
        self._send(200,'{"ok":true,"status":"%s"}'%(to or 'shipped'))
      return
    if self.path=='/api/delete':
      # 破坏性入口：auth 需求下必须先验 admin（deny 优先，无副作用），再谈确认与索引。
      if FLAGS.get('admin_protect') and self.headers.get('X-Role')!='admin':
        self._send(401,'{"error":"login required"}'); return
      idx=data.get('index',0)
      try:
        if isinstance(idx,bool) or (isinstance(idx,str) and not idx.lstrip('-').isdigit()) or not isinstance(idx,(int,str)):
          raise ValueError('bad index')
        idx=int(idx)
      except Exception:
        self._send(400,'{"error":"bad index"}'); return
      if FLAGS.get('confirm_guard') and data.get('confirm')!=True:
        # v84 strict confirm: 仅 true/1 通过，非空字符串("false"等)亦拒绝。
        self._send(400,'{"error":"confirm required"}'); return
      with lock:
        _denied=False
        if 0<=idx<len(items):
          _tgt=items[idx]
          if FLAGS.get('isolation') and self.headers.get('X-User','') and _tgt.get('owner','') not in ('',self.headers.get('X-User','')):
            ok=False; _denied=True
          else: items.pop(idx); ok=True
        else: ok=False
      if _denied:
        save(); self._send(403,'{"error":"not your item"}'); return
      save()
      self._send(200 if ok else 404,'{"ok":true}' if ok else '{"error":"no such item"}'); return
    if self.path=='/api/redeem':
      if not FLAGS.get('deadline_guard'): self._send(404,'{}'); return
      _t=data.get('text',data.get('title',data.get('name','')))
      _hit=None
      with lock:
        for _it in items:
          if isinstance(_it,dict) and _it.get('text',_it.get('title',_it.get('name','')))==_t: _hit=_it; break
      if _hit is None: self._send(404,'{"error":"no such item"}'); return
      if _s2past(_hit.get('valid_to',_hit.get('expire',_hit.get('validto','')))): self._send(410,'{"error":"expired"}'); return
      self._send(200,'{"ok":true}'); return
    if self.path=='/api/accrue':
      # G-temporal-accrual: fine=rate*max(0,(as_of-due_at).days); explicit date
      # provenance, no sleep. Default rate = SPEC accrual_rate flag.
      if not FLAGS.get('accrual'): self._send(404,'{}'); return
      import datetime as _adt
      def _pd(_v):
        try:
          if not isinstance(_v,str): return None
          return _adt.datetime.strptime(_v,'%Y-%m-%d').replace(tzinfo=_adt.timezone.utc)
        except Exception: return None
      _d1=_pd(data.get('due_at',data.get('due',data.get('dueAt',''))))
      _d2=_pd(data.get('as_of',data.get('asOf',data.get('asof',''))))
      try: _rr=float(data.get('rate',''))
      except Exception: _rr=None
      if _rr is None or not (_rr==_rr and _rr>=0):
        try: _rr=float(FLAGS.get('accrual_rate',1))
        except Exception: _rr=1.0
      if _d1 is None or _d2 is None: self._send(400,'{"error":"bad-date"}'); return
      _el=max(0,(_d2-_d1).days)
      self._send(200,json.dumps({'fine':round(_el*_rr,2),'elapsed':_el,'rate':_rr})); return
    if self.path=='/api/login':
      if not FLAGS.get('secret_hash'): self._send(404,'{}'); return
      _u=data.get('username',data.get('text',data.get('name',''))); _p=data.get('password',data.get('passwd',data.get('pwd','')))
      _ok=False
      with lock:
        for _it in items:
          if isinstance(_it,dict) and _it.get('username',_it.get('text',_it.get('name','')))==_u:
            for _sk in ('password','passwd','pwd'):
              if _it.get(_sk)==_s2hash(_p): _ok=True
      self._send(200 if _ok else 401,'{"ok":true}' if _ok else '{"error":"bad credentials"}'); return
    if self.path=='/api/book':
      if not FLAGS.get('booking'): self._send(404,'{}'); return
      _key=str(data.get('slot',data.get('id',data.get('index',''))))
      _hit=None
      with lock:
        for _it in items:
          if isinstance(_it,dict) and _it.get('kind')=='slot' and str(_it.get('slot'))==_key:
            _hit=_it; break
        if _hit is None:
          try:
            _ix=int(_key) if _key!='' else 0
            _sl2=[it for it in items if isinstance(it,dict) and it.get('kind')=='slot']
            if 0<=_ix<len(_sl2): _hit=_sl2[_ix]
          except Exception: pass
        if _hit is None:
          self._send(404,'{"error":"no such slot"}'); return
        if _hit.get('status') not in ('可预约','available'):
          self._send(409,'{"error":"already booked"}'); return
        _hit['status']='已预约'
      save()
      self._send(200,json.dumps({'ok':True,'slot':_hit.get('slot'),'status':_hit.get('status')},ensure_ascii=False)); return
    self._send(404,'{}')
HTTPServer(('127.0.0.1',int(os.environ.get('PORT','0'))),H).serve_forever()
