// Manifold AI Optimization System — Dashboard
// Designed for non-engineering audiences

const S = {
  metrics: { cpu:{usagePct:0}, memory:{usedBytes:0,totalBytes:1}, gpus:[], ts:null },
  metricHistory: { cpu:[], gpus:{} },
  job: null,
  heartbeat: { enabled:false, ts:null, summary:null, error:null },
  cfdStatus: null,
  diagReport: null,
  diagLoading: false,
}

const fmtPct = v => isFinite(+v) ? `${Math.round(+v)}%` : '-'
const fmtGB  = b => isFinite(+b)&&+b>0 ? `${(+b/1e9).toFixed(1)} GB` : '-'
const fmtK   = v => isFinite(+v) ? (+v>999?`${(+v/1000).toFixed(1)}k`:String(Math.round(+v))) : '-'
const clamp  = (v,lo,hi) => Math.max(lo,Math.min(hi,v))

function el(tag, attrs={}, ...children){
  const e=document.createElement(tag)
  for(const[k,v] of Object.entries(attrs)){
    if(k==='class')e.className=v
    else if(k==='style')Object.assign(e.style,v)
    else if(k==='text')e.textContent=v
    else if(k==='html')e.innerHTML=v
    else if(k.startsWith('on'))e.addEventListener(k.slice(2),v)
    else e.setAttribute(k,v)
  }
  for(const c of children.flat()){
    if(c==null)continue
    e.appendChild(typeof c==='string'?document.createTextNode(c):c)
  }
  return e
}

const PIPELINE_STEPS = [
  { id:'bo',      icon:'🧠', name:'贝叶斯优化 AI', desc:'根据历史数据智能推荐下一批待测参数', explain:'就像做过数百次实验的专家：根据已有数据判断哪里最有可能更好，而非随机猜测' },
  { id:'cfd',     icon:'🌀', name:'物理仿真',       desc:'并行运行 OpenFOAM 流体力学数值计算', explain:'用计算机模拟气体在歧管内的分配与压力损失，替代反复试制' },
  { id:'llm',     icon:'💬', name:'AI 分析',        desc:'大语言模型解读仿真结果并提出改进方向', explain:'Qwen 将一堆数字翻译成人类可理解的结论和建议' },
  { id:'anomaly', icon:'🔍', name:'异常检测',       desc:'自动识别数值发散或可疑结果', explain:'防止错误的仿真结果误导优化方向，需要时自动暂停并提示' },
  { id:'report',  icon:'📊', name:'报告生成',       desc:'优化完成后输出最优设计和完整分析', explain:'汇总整个搜索过程，给出最优参数和工程建议' },
]

function connectWS(){
  const proto=location.protocol==='https:'?'wss':'ws'
  const base=`${proto}://${location.host}`

  const wsM=new WebSocket(`${base}/ws/metrics?interval=1`)
  wsM.onmessage=ev=>{
    try{
      const m=JSON.parse(ev.data)
      S.metrics=m
      // Show data source in topbar
      const srcEl=document.getElementById('metrics-source')
      if(srcEl)srcEl.textContent=m._source||''
      S.metricHistory.cpu.push(m.cpu.usagePct)
      if(S.metricHistory.cpu.length>120)S.metricHistory.cpu.shift()
      for(const g of(m.gpus||[])){
        const k='gpu'+g.index
        if(!S.metricHistory.gpus[k])S.metricHistory.gpus[k]=[]
        S.metricHistory.gpus[k].push(g.usagePct)
        if(S.metricHistory.gpus[k].length>120)S.metricHistory.gpus[k].shift()
      }
      renderHardware()
    }catch(e){console.error('metrics ws error',e)}
  }
  wsM.onerror=ev=>console.error('metrics ws error',ev)
  wsM.onopen=()=>console.log('metrics ws connected')

  const wsJ=new WebSocket(`${base}/ws/job?interval=2`)
  wsJ.onmessage=ev=>{
    try{
      S.job=JSON.parse(ev.data)
      renderOptimization()
      renderPipeline()
    }catch(e){}
  }

  const wsH=new WebSocket(`${base}/ws/heartbeat?interval=1`)
  wsH.onmessage=ev=>{
    try{
      S.heartbeat=JSON.parse(ev.data)
      renderHeartbeat()
    }catch(e){}
  }
}

function buildLayout(){
  const app=document.getElementById('app')
  app.innerHTML=''

  const storyHtml='我们正在用 <b>AI 自动寻找最优的歧管分流设计</b>。歧管把气体从一个总入口分配到多个出口，核心问题是：<b>如何让各出口流量尽可能均匀，同时把压降降到最低？</b><br><br>传统方法需要工程师手工试很多种几何/导流方案，耗时很长。这套系统用 <b>贝叶斯优化 AI + OpenFOAM 物理仿真 + Qwen 分析</b>，自动探索参数空间，持续逼近更好的分流效果。'

  app.appendChild(
    el('div',{class:'layout'},
      el('div',{class:'topbar'},
        el('span',{class:'topbar-logo'},'歧管 AI 寻优系统'),
        el('span',{id:'status-dot',class:'dot idle'}),
        el('span',{id:'status-text',class:'topbar-sub'},'连接中…'),
        el('span',{id:'metrics-source',style:{fontSize:'10px',color:'var(--muted2)',marginLeft:'auto',opacity:'0.7'}},''),
      ),
      el('div',{class:'page'},
        el('div',{class:'story-banner'},
          el('div',{class:'story-icon'},'🔬'),
          el('div',{},
            el('div',{class:'story-title'},'这台机器在做什么？'),
            el('div',{class:'story-body',html:storyHtml}),
          ),
        ),
        el('div',{class:'section-label'},'AI 工作流程'),
        el('div',{id:'pipeline-wrap'},buildPipeline(null)),
        el('div',{class:'divider'}),
        el('div',{class:'section-label'},'AI 巡检（每秒）'),
        el('div',{id:'heartbeat-wrap'},
          el('div',{class:'heartbeat-card'},
            el('div',{class:'heartbeat-head'},
              el('div',{class:'heartbeat-title'},
                el('span',{},'🧭'),
                el('span',{},'巡检摘要'),
                el('span',{id:'heartbeat-badge',class:'heartbeat-badge'},'未启用'),
              ),
              el('div',{id:'heartbeat-ts',class:'heartbeat-ts'},''),
            ),
            el('div',{id:'heartbeat-body',class:'heartbeat-body',text:'等待 Qwen 巡检…'}),
          )
        ),
        el('div',{class:'divider'}),
        el('div',{class:'section-label'},'Qwen 智能诊断'),
        el('div',{id:'diag-wrap'},
          el('div',{style:{color:'var(--muted2)',padding:'16px 0'},text:'点击"启动诊断"让 Qwen 检查 OpenFOAM 状态…'}),
        ),
        el('div',{class:'divider'}),
        el('div',{class:'section-label'},'物理仿真状态'),
        el('div',{id:'cfd-wrap'},
          el('div',{style:{color:'var(--muted2)',padding:'16px 0'},text:'加载中…'}),
        ),
        el('div',{class:'divider'}),
        el('div',{class:'section-label'},'算力资源分配'),
        el('div',{id:'hw-grid',class:'hw-grid'},
          el('div',{class:'hw-card'},el('div',{style:{color:'var(--muted2)'},text:'加载中…'})),
        ),
        el('div',{class:'divider'}),
        el('div',{class:'section-label'},'优化进展'),
        el('div',{id:'opt-wrap'},
          el('div',{style:{color:'var(--muted2)',padding:'16px 0'},text:'等待优化任务数据…'}),
        ),
      ),
    )
  )
}

function triggerDiagnosis(autoLaunch){
  if(S.diagLoading)return
  S.diagLoading=true
  renderDiagReport()
  fetch('/api/qwen-diagnose?auto_launch='+(autoLaunch?'true':'false'),{method:'POST'})
    .then(function(r){return r.json()})
    .then(function(d){
      S.diagReport=d
      S.diagLoading=false
      renderDiagReport()
      // Also refresh CFD status after diagnosis (OF may have been launched)
      setTimeout(function(){
        fetch('/api/cfd-status/refresh',{method:'POST'})
          .then(function(r){return r.json()})
          .then(function(d){S.cfdStatus=d;renderCFDStatus()})
      }, 5000)
    })
    .catch(function(e){
      S.diagLoading=false
      renderDiagReport()
    })
}

function renderDiagReport(){
  const wrap=document.getElementById('diag-wrap')
  if(!wrap)return
  const rpt=S.diagReport
  const loading=S.diagLoading

  const card=el('div',{class:'diag-card'})

  // Header
  const head=el('div',{class:'diag-head'})
  const statusBadge=el('span',{class:'diag-badge'+(loading?' loading':rpt?(rpt.of_running?' of-running':' of-stopped'):'')},
    loading?'⏳ 分析中…':rpt?(rpt.of_running?'▶ OF 运行中':'⚠ OF 未运行'):'待诊断')
  head.appendChild(el('div',{class:'diag-title'},
    el('span',{},'🤖'),
    el('span',{},'Qwen 诊断报告'),
    statusBadge,
  ))
  if(rpt&&rpt.ts){
    head.appendChild(el('div',{class:'diag-ts'},`诊断时间：${rpt.ts}`))
  }
  card.appendChild(head)

  if(loading){
    card.appendChild(el('div',{class:'diag-loading'},
      el('span',{class:'spin'},'⚙'),
      el('span',{style:{marginLeft:'10px'}},'Qwen 正在分析 OpenFOAM 状态，请稍候…（约 15-30 秒）'),
    ))
  } else if(rpt){
    // SSH error
    if(rpt.ssh_error){
      card.appendChild(el('div',{class:'diag-error'},`SSH 错误：${rpt.ssh_error}`))
    }

    // OF processes
    if(rpt.of_running&&rpt.foam_procs&&rpt.foam_procs.length>0){
      const procBox=el('div',{class:'diag-section'})
      procBox.appendChild(el('div',{class:'diag-section-title'},'运行中的进程'))
      rpt.foam_procs.slice(0,3).forEach(function(p){
        procBox.appendChild(el('div',{class:'diag-proc-line'},p.substring(0,120)))
      })
      card.appendChild(procBox)
    }

    // Qwen analysis
    if(rpt.qwen_analysis){
      const anaBox=el('div',{class:'diag-section'})
      anaBox.appendChild(el('div',{class:'diag-section-title'},'原因分析'))
      anaBox.appendChild(el('div',{class:'diag-analysis'},rpt.qwen_analysis))
      card.appendChild(anaBox)
    }

    // Qwen plan
    if(rpt.qwen_plan){
      const planBox=el('div',{class:'diag-section'})
      planBox.appendChild(el('div',{class:'diag-section-title'},'Debug 计划'))
      const lines=rpt.qwen_plan.split('\n').filter(function(l){return l.trim()})
      const ol=el('ol',{class:'diag-plan-list'})
      lines.forEach(function(l){
        const clean=l.replace(/^\d+[\.\、\)]\s*/,'')
        ol.appendChild(el('li',{},clean))
      })
      planBox.appendChild(ol)
      card.appendChild(planBox)
    }

    // Suggested command
    if(rpt.qwen_action){
      const cmdBox=el('div',{class:'diag-section'})
      cmdBox.appendChild(el('div',{class:'diag-section-title'},'建议命令'))
      cmdBox.appendChild(el('div',{class:'diag-cmd'},rpt.qwen_action))
      card.appendChild(cmdBox)
    }

    // Actions taken
    if(rpt.actions_taken&&rpt.actions_taken.length>0){
      const actBox=el('div',{class:'diag-section'})
      actBox.appendChild(el('div',{class:'diag-section-title'},'已执行操作'))
      const ul=el('ul',{class:'diag-actions-list'})
      rpt.actions_taken.forEach(function(a){
        ul.appendChild(el('li',{class:'diag-action-item'},a))
      })
      actBox.appendChild(ul)
      card.appendChild(actBox)
    }

    // Log snippet (errors only)
    if(rpt.error_snippet){
      const logBox=el('div',{class:'diag-section'})
      logBox.appendChild(el('div',{class:'diag-section-title'},'错误片段'))
      logBox.appendChild(el('pre',{class:'diag-log'},rpt.error_snippet.substring(0,600)))
      card.appendChild(logBox)
    }
  } else {
    card.appendChild(el('div',{class:'diag-hint'},'点击下方按钮，让 Qwen 检查服务器 OpenFOAM 运行状态并给出诊断报告。'))
  }

  // Footer buttons
  const footer=el('div',{class:'diag-footer'})
  footer.appendChild(el('button',{
    class:'diag-btn primary'+(loading?' disabled':''),
    onclick:function(){if(!S.diagLoading)triggerDiagnosis(true)}
  },'🚀 诊断并自动启动 OF'))
  footer.appendChild(el('button',{
    class:'diag-btn'+(loading?' disabled':''),
    onclick:function(){if(!S.diagLoading)triggerDiagnosis(false)}
  },'🔍 仅诊断'))
  card.appendChild(footer)

  wrap.innerHTML=''
  wrap.appendChild(card)
}

// Fetch existing report on page load
function loadDiagReport(){
  fetch('/api/qwen-diagnose')
    .then(function(r){return r.json()})
    .then(function(d){
      if(d&&d.ts){S.diagReport=d;renderDiagReport()}
      else{renderDiagReport()}
    })
    .catch(function(){renderDiagReport()})
}

function renderCFDStatus(){
  const wrap=document.getElementById('cfd-wrap')
  if(!wrap)return
  const st=S.cfdStatus
  if(!st){
    wrap.innerHTML='<div style="color:var(--muted2);padding:16px 0">加载中…</div>'
    return
  }
  const card=el('div',{class:'cfd-card'})

  // ── Header ──
  const head=el('div',{class:'cfd-head'})
  const badgeTxt=st.n_running>0?`${st.n_running} 个运行中`:'无运行工况'
  head.appendChild(el('div',{class:'cfd-title'},
    el('span',{},'🌀'),
    el('span',{},'仿真工况'),
    el('span',{class:'cfd-badge '+(st.n_running>0?'active':'')},badgeTxt),
  ))
  head.appendChild(el('div',{class:'cfd-ts'},
    el('span',{},`更新：${st.ts||'—'}`),
    el('span',{style:{marginLeft:'12px'}},`下次：${st.next_refresh_ts||'—'}`),
  ))
  card.appendChild(head)

  // ── Error ──
  if(!st.ok&&st.error){
    card.appendChild(el('div',{class:'cfd-error'},`⚠ ${st.error}`))
  }

  // ── Shared info ──
  if(st.n_running>0||(st.ok&&st.solver&&st.solver!=='unknown')){
    card.appendChild(el('div',{class:'cfd-info-grid'},
      el('div',{class:'cfd-info-item'},
        el('div',{class:'cfd-info-label'},'求解器'),
        el('div',{class:'cfd-info-val'},st.solver||'—'),
      ),
      el('div',{class:'cfd-info-item'},
        el('div',{class:'cfd-info-label'},'湍流模型'),
        el('div',{class:'cfd-info-val'},st.turb_model||'—'),
      ),
      el('div',{class:'cfd-info-item'},
        el('div',{class:'cfd-info-label'},'并行核数'),
        el('div',{class:'cfd-info-val'},st.cores_per_case>0?`${st.cores_per_case} 核`:'—'),
      ),
      el('div',{class:'cfd-info-item'},
        el('div',{class:'cfd-info-label'},'版本'),
        el('div',{class:'cfd-info-val'},st.of_version||'—'),
      ),
    ))
  }

  // ── Per-case table ──
  if(st.cases&&st.cases.length>0){
    const table=el('table',{class:'cfd-table'})
    const thead=el('thead')
    thead.innerHTML='<tr><th>工况</th><th>网格数</th><th>当前步</th></tr>'
    table.appendChild(thead)
    const tbody=el('tbody')
    for(const c of st.cases){
      const tr=el('tr')
      tr.innerHTML=`<td>${c.case_id}</td><td>${c.n_cells!=null?fmtK(c.n_cells):'—'}</td><td>${c.current_step??'—'}</td>`
      tbody.appendChild(tr)
    }
    table.appendChild(tbody)
    card.appendChild(table)
  }else if(st.ok&&st.n_running===0){
    card.appendChild(el('div',{class:'cfd-empty'},'当前没有运行中的 OpenFOAM 工况'))
  }

  // ── Refresh button ──
  card.appendChild(el('div',{class:'cfd-footer'},
    el('button',{class:'cfd-refresh-btn',onclick:function(){
      fetch('/api/cfd-status/refresh',{method:'POST'})
        .then(function(r){return r.json()})
        .then(function(d){S.cfdStatus=d;renderCFDStatus()})
        .catch(function(){})
    }},'↻ 立即刷新'),
  ))

  wrap.innerHTML=''
  wrap.appendChild(card)
}

function startCFDPolling(){
  function fetchCFD(){
    fetch('/api/cfd-status')
      .then(function(r){return r.json()})
      .then(function(d){S.cfdStatus=d;renderCFDStatus()})
      .catch(function(){})
  }
  fetchCFD()
  setInterval(fetchCFD,60000)
}

function renderHeartbeat(){
  const w=document.getElementById('heartbeat-wrap')
  if(!w)return
  const b=document.getElementById('heartbeat-body')
  const ts=document.getElementById('heartbeat-ts')
  const badge=document.getElementById('heartbeat-badge')
  const hb=S.heartbeat||{}
  if(b)b.textContent=hb.summary||hb.error||'等待 Qwen 巡检…'
  if(ts)ts.textContent=hb.ts||''
  if(badge){
    badge.textContent=hb.enabled?'已启用':'未启用'
  }
}

function buildPipeline(activeStep){
  const wrap=el('div',{class:'pipeline'})
  const isRunning=activeStep!==null
  const activeIdx=PIPELINE_STEPS.findIndex(s=>s.id===activeStep)
  PIPELINE_STEPS.forEach((step,i)=>{
    const isActive=step.id===activeStep
    const isDone=isRunning&&activeIdx>i
    let cls='pipe-box'+(isActive?' active':(isDone?' done':''))
    const box=el('div',{class:cls},
      el('div',{class:'pipe-icon'+(isActive?' pulse':'')},step.icon),
      el('div',{class:'pipe-name'},step.name),
      el('div',{class:'pipe-desc'},step.desc),
    )
    // When optimization is running: ALL boxes show "运行中"
    // The currently active one gets the bright green badge; others get a dim badge
    if(isRunning){
      if(isActive){
        box.appendChild(el('div',{class:'pipe-badge run'},'▶ 运行中'))
      }else if(!isDone){
        box.appendChild(el('div',{class:'pipe-badge run-dim'},'▶ 运行中'))
      }else{
        box.appendChild(el('div',{class:'pipe-badge done-badge'},'✓ 已完成'))
      }
    }
    const stepEl=el('div',{class:'pipe-step'},box,
      el('div',{style:{fontSize:'10px',color:'var(--muted2)',textAlign:'center',maxWidth:'110px',lineHeight:'1.4',marginTop:'6px'}},step.explain),
    )
    wrap.appendChild(stepEl)
    if(i<PIPELINE_STEPS.length-1)wrap.appendChild(el('div',{class:'pipe-arrow'},'→'))
  })
  return wrap
}

function renderPipeline(){
  const wrap=document.getElementById('pipeline-wrap')
  if(!wrap)return
  const job=S.job
  let activeStep=null
  if(job&&job.status==='running'){
    const phase=(job.current_phase||'').toLowerCase()
    if(phase.includes('suggest')||phase.includes('bo')||phase.includes('init'))activeStep='bo'
    else if(phase.includes('eval')||phase.includes('cfd')||phase.includes('foam'))activeStep='cfd'
    else if(phase.includes('llm')||phase.includes('report'))activeStep='llm'
    else if(phase.includes('anomaly'))activeStep='anomaly'
    else if(phase.includes('done')||phase.includes('finish'))activeStep='report'
    else activeStep='bo'
  }
  wrap.innerHTML=''
  wrap.appendChild(buildPipeline(activeStep))
  const dot=document.getElementById('status-dot')
  const txt=document.getElementById('status-text')
  if(dot&&txt){
    const running=job&&job.status==='running'
    dot.className=running?'dot':'dot idle'
    if(running){
      txt.textContent='优化进行中 · 第 '+(job.iteration||'?')+' 轮 · 已评估 '+(job.evaluated||0)+' / '+(job.budget||'?')+' 个方案'
    }else{
      txt.textContent=job?'优化已完成':'等待任务启动…'
    }
  }
}

function renderHardware(){
  const grid=document.getElementById('hw-grid')
  if(!grid)return
  const m=S.metrics
  const cards=[]
  const gpus=m.gpus||[]
  if(gpus.length===0){
    cards.push(buildHwCard({chip:'gpu',chipLabel:'GPU',name:'未检测到显卡',role:'大语言模型推理',pct:0,usedBytes:0,totalBytes:1,task:'请确认 nvidia-smi 可访问',barClass:'gpu-bar'}))
  }else{
    gpus.forEach(function(g){
      const hist=S.metricHistory.gpus['gpu'+g.index]||[]
      let task
      if(g.usagePct>60)task='<b>正在运行 Qwen</b>，处理语言理解与报告生成'
      else if(g.usagePct>10)task='<b>待命中</b>，语言模型已加载'
      else task='<b>空闲</b>，等待推理请求'
      cards.push(buildHwCard({chip:'gpu',chipLabel:'GPU '+g.index,name:g.name,role:'语言模型推理加速卡',pct:g.usagePct,usedBytes:g.memUsedBytes,totalBytes:g.memTotalBytes,tempC:g.tempC,task:task,history:hist,barClass:'gpu-bar'}))
    })
  }
  const cpuPct=m.cpu.usagePct||0
  const cfdRunning=S.cfdStatus&&S.cfdStatus.n_running>0
  let cpuTask
  if(cpuPct>70)cpuTask=cfdRunning?'多个 <b>OpenFOAM 仿真</b> 正在并行运行（高负载为正常状态）':'<b>高负载运行中</b>'
  else if(cpuPct>20)cpuTask=cfdRunning?'<b>OpenFOAM 仿真</b> 运行中':'<b>运行中</b>'
  else cpuTask='<b>空闲</b>，等待下一批仿真任务'
  cards.push(buildHwCard({chip:'cpu',chipLabel:'CPU',name:'2× EPYC 9754（512 线程）',role:'流体力学数值仿真引擎',pct:cpuPct,usedBytes:null,totalBytes:null,tempC:m.cpu&&m.cpu.tempC!=null?m.cpu.tempC:null,task:cpuTask,history:S.metricHistory.cpu,barClass:'cpu-bar',extraMeta:'≈'+Math.round(cpuPct*5.12)+'/512 线程'}))
  const memPct=m.memory.totalBytes>0?(m.memory.usedBytes/m.memory.totalBytes)*100:0
  cards.push(buildHwCard({chip:'ram',chipLabel:'RAM',name:'系统内存',role:'仿真数据与模型缓存',pct:memPct,usedBytes:m.memory.usedBytes,totalBytes:m.memory.totalBytes,task:'已用 <b>'+fmtGB(m.memory.usedBytes)+'</b> / '+fmtGB(m.memory.totalBytes),barClass:'ram-bar'}))
  grid.innerHTML=''
  cards.forEach(function(c){grid.appendChild(c)})
}

function buildHwCard(opts){
  const chip=opts.chip,chipLabel=opts.chipLabel,name=opts.name,role=opts.role
  const pct=opts.pct,usedBytes=opts.usedBytes,totalBytes=opts.totalBytes
  const task=opts.task,history=opts.history,barClass=opts.barClass,extraMeta=opts.extraMeta
  const tempC=opts.tempC
  const card=el('div',{class:'hw-card'})
  const hdr=el('div',{class:'hw-header'},el('span',{class:'hw-chip '+chip,text:chipLabel}),el('span',{class:'hw-name',text:name}))
  if(tempC!=null){
    const tc=Math.round(tempC)
    let tcls='temp-badge'
    if(tc>=85)tcls+=' temp-hot'
    else if(tc>=70)tcls+=' temp-warm'
    hdr.appendChild(el('span',{class:tcls},tc+'°C'))
  }
  card.appendChild(hdr)
  card.appendChild(el('div',{class:'hw-role',text:role}))
  const bw=el('div',{class:'hw-bar-wrap'})
  const bar=el('div',{class:'hw-bar '+(barClass||'gpu-bar')})
  bar.style.width=clamp(pct||0,0,100)+'%'
  bw.appendChild(bar)
  card.appendChild(bw)
  const meta=el('div',{class:'hw-meta'})
  meta.appendChild(el('span',{text:extraMeta||fmtPct(pct)}))
  if(usedBytes!=null&&totalBytes!=null&&totalBytes>0)meta.appendChild(el('span',{text:fmtGB(usedBytes)+' / '+fmtGB(totalBytes)}))
  card.appendChild(meta)
  if(history&&history.length>2){
    let color
    if(barClass==='gpu-bar')color='rgba(99,102,241,0.8)'
    else if(barClass==='cpu-bar')color='rgba(34,211,238,0.8)'
    else color='rgba(167,139,250,0.8)'
    card.appendChild(buildMiniChart(history,color))
  }
  const td=el('div',{class:'hw-task',html:task})
  card.appendChild(td)
  return card
}

function buildMiniChart(values,color){
  const canvas=el('canvas',{style:{width:'100%',height:'36px',display:'block',marginTop:'10px'}})
  canvas.width=300
  canvas.height=36
  requestAnimationFrame(function(){
    const ctx=canvas.getContext('2d')
    const w=canvas.width,h=canvas.height
    const v=values.slice(-60)
    if(v.length<2)return
    const grad=ctx.createLinearGradient(0,0,0,h)
    grad.addColorStop(0,color.replace('0.8','0.25'))
    grad.addColorStop(1,color.replace('0.8','0.02'))
    ctx.beginPath()
    ctx.moveTo(0,h)
    for(let i=0;i<v.length;i++){
      const x=(i/(v.length-1))*w
      const y=h-clamp(v[i]/100,0,1)*h
      if(i===0)ctx.lineTo(x,y)
      else ctx.lineTo(x,y)
    }
    ctx.lineTo(w,h)
    ctx.closePath()
    ctx.fillStyle=grad
    ctx.fill()
    ctx.beginPath()
    for(let i=0;i<v.length;i++){
      const x=(i/(v.length-1))*w
      const y=h-clamp(v[i]/100,0,1)*h
      if(i===0)ctx.moveTo(x,y)
      else ctx.lineTo(x,y)
    }
    ctx.strokeStyle=color
    ctx.lineWidth=1.5
    ctx.stroke()
  })
  return canvas
}

function renderOptimization(){
  const wrap=document.getElementById('opt-wrap')
  if(!wrap)return
  const job=S.job
  if(!job)return
  const evaluated=job.evaluated||0
  const budget=job.budget||0
  const bestObj=job.best_objective
  const bestCV=job.best_flow_cv
  const bestDP=job.best_pressure_drop
  const pct=budget>0?Math.round((evaluated/budget)*100):0

  const inner=el('div')
  inner.id='opt-inner-repl'

  inner.appendChild(el('div',{class:'opt-grid'},
    el('div',{class:'stat-card'},
      el('div',{class:'stat-label'},'已评估方案数'),
      el('div',{class:'stat-val'},fmtK(evaluated),el('span',{class:'stat-unit'},'/ '+fmtK(budget))),
      el('div',{class:'stat-sub'},'完成 '+pct+'%'),
    ),
    el('div',{class:'stat-card'},
      el('div',{class:'stat-label'},'当前最优流量均匀性 (CV)'),
      el('div',{class:'stat-val',style:{color:'var(--green)'}},bestCV!=null?(+bestCV).toFixed(4):'—'),
      el('div',{class:'stat-sub'},'越小越均匀 · ΔP='+(bestDP!=null?(+bestDP).toFixed(1)+'Pa':'—')+' · obj='+(bestObj!=null?(+bestObj).toFixed(4):'—')),
    ),
    el('div',{class:'stat-card'},
      el('div',{class:'stat-label'},'当前轮次'),
      el('div',{class:'stat-val'},String(job.iteration||'—')),
      el('div',{class:'stat-sub'},'每轮并行 '+(job.batch_size||'?')+' 个方案'),
    ),
  ))

  inner.appendChild(el('div',{style:{marginBottom:'4px',fontSize:'12px',color:'var(--muted)'}},'整体进度：'+pct+'%'))
  const pbW=el('div',{class:'progress-bar-wrap'})
  pbW.appendChild(el('div',{class:'progress-bar',style:{width:pct+'%'}}))
  inner.appendChild(pbW)

  const twoCol=el('div',{class:'two-col',style:{marginTop:'20px'}})

  if(job.best_params){
    const p=job.best_params
    const l1=p.logit_1!=null?(+p.logit_1).toFixed(3):null
    const l2=p.logit_2!=null?(+p.logit_2).toFixed(3):null
    const l3=p.logit_3!=null?(+p.logit_3).toFixed(3):null
    const softmax=function(xs){
      const m=Math.max.apply(null,xs)
      const ex=xs.map(x=>Math.exp(x-m))
      const s=ex.reduce((a,b)=>a+b,0)
      return ex.map(e=>e/s)
    }
    const ws=(l1!=null&&l2!=null&&l3!=null)?softmax([+l1,+l2,+l3,0]):null
    const wTxt=ws?ws.map((w,i)=>`w${i+1}=${(w*100).toFixed(1)}%`).join(' · '):'—'
    twoCol.appendChild(el('div',{class:'best-card'},
      el('div',{class:'best-title'},'✦ 当前找到的最优设计'),
      el('div',{class:'best-params'},
        el('div',{class:'best-param'},el('div',{class:'best-param-val'},l1??'—'),el('div',{class:'best-param-label'},'logit_1'),el('div',{class:'best-param-explain'},'出口开口分配参数')),
        el('div',{class:'best-param'},el('div',{class:'best-param-val'},l2??'—'),el('div',{class:'best-param-label'},'logit_2'),el('div',{class:'best-param-explain'},'出口开口分配参数')),
        el('div',{class:'best-param'},el('div',{class:'best-param-val'},l3??'—'),el('div',{class:'best-param-label'},'logit_3'),el('div',{class:'best-param-explain'},wTxt)),
      ),
    ))
  }else{
    twoCol.appendChild(el('div',{class:'best-card'},
      el('div',{class:'best-title'},'最优设计'),
      el('div',{style:{color:'var(--muted2)'},text:'正在积累初始数据…'}),
    ))
  }

  const trendCard=el('div',{class:'trend-wrap'})
  trendCard.appendChild(el('div',{class:'trend-explain',html:'<b>优化收敛曲线</b> — 每个点代表一批仿真完成后当前最优目标值。<b>曲线持续上升说明 AI 正在找到更好的分流设计。</b>'}))
  const trendCanvas=el('canvas',{style:{width:'100%',height:'140px'}})
  trendCanvas.width=600
  trendCanvas.height=140
  trendCard.appendChild(trendCanvas)
  twoCol.appendChild(trendCard)
  inner.appendChild(twoCol)

  if(job.history&&job.history.length>1){
    const scatterWrap=el('div',{class:'scatter-wrap',style:{marginTop:'20px'}})
    scatterWrap.appendChild(el('div',{class:'scatter-explain',html:'<b>参数空间探索图（logit_1 vs logit_2）</b> — 每个点代表一个已测试的方案。<b>越亮绿的点目标值越高</b>（分流更好）。'}))
    const sc=el('canvas',{style:{width:'100%',height:'200px'}})
    sc.width=800
    sc.height=200
    scatterWrap.appendChild(sc)
    inner.appendChild(scatterWrap)
    requestAnimationFrame(function(){drawScatter(sc,job.history)})
  }

  const existing=document.getElementById('opt-inner-repl')
  if(existing)existing.replaceWith(inner)
  else{wrap.innerHTML='';wrap.appendChild(inner)}

  requestAnimationFrame(function(){drawTrend(trendCanvas,job.history)})
}

function drawTrend(canvas,history){
  if(!canvas||!history||history.length===0)return
  const ctx=canvas.getContext('2d')
  const W=canvas.width,H=canvas.height
  ctx.clearRect(0,0,W,H)
  const vals=[]
  for(const r of history){
    if(r.objective!=null&&isFinite(r.objective))vals.push(r.objective)
  }
  if(vals.length<2)return
  let runMax=-Infinity
  const bests=vals.map(function(v){runMax=Math.max(runMax,v);return runMax})
  const mn=Math.min.apply(null,vals)*0.9
  const mx=Math.max.apply(null,vals)*1.05
  const pad={l:10,r:10,t:10,b:10}
  const W_=W-pad.l-pad.r,H_=H-pad.t-pad.b
  const toX=function(i){return pad.l+(i/(vals.length-1))*W_}
  const toY=function(v){return pad.t+H_-((v-mn)/(mx-mn))*H_}
  ctx.strokeStyle='rgba(255,255,255,0.05)'
  ctx.lineWidth=1
  for(let i=0;i<=4;i++){
    const y=pad.t+(i/4)*H_
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(pad.l+W_,y);ctx.stroke()
  }
  ctx.fillStyle='rgba(99,102,241,0.35)'
  vals.forEach(function(v,i){ctx.beginPath();ctx.arc(toX(i),toY(v),3,0,Math.PI*2);ctx.fill()})
  const grad=ctx.createLinearGradient(pad.l,0,pad.l+W_,0)
  grad.addColorStop(0,'rgba(99,102,241,0.8)')
  grad.addColorStop(1,'rgba(74,222,128,0.9)')
  ctx.beginPath()
  bests.forEach(function(v,i){
    const x=toX(i),y=toY(v)
    if(i===0)ctx.moveTo(x,y)
    else ctx.lineTo(x,y)
  })
  ctx.strokeStyle=grad
  ctx.lineWidth=2.5
  ctx.stroke()
  const last=bests.length-1
  ctx.beginPath()
  ctx.arc(toX(last),toY(bests[last]),5,0,Math.PI*2)
  ctx.fillStyle='#4ade80'
  ctx.fill()
}

function drawScatter(canvas,history){
  if(!canvas||!history||history.length===0)return
  const ctx=canvas.getContext('2d')
  const W=canvas.width,H=canvas.height
  ctx.clearRect(0,0,W,H)
  const pts=[]
  for(const r of history){
    if(r.params&&r.params.logit_1!=null&&r.params.logit_2!=null&&r.objective!=null&&isFinite(r.objective))pts.push(r)
  }
  if(pts.length===0)return
  const xs=pts.map(function(r){return r.params.logit_1})
  const ys=pts.map(function(r){return r.params.logit_2})
  const objs=pts.map(function(r){return r.objective})
  const mnX=Math.min.apply(null,xs),mxX=Math.max.apply(null,xs)
  const mnY=Math.min.apply(null,ys),mxY=Math.max.apply(null,ys)
  const mnO=Math.min.apply(null,objs),mxO=Math.max.apply(null,objs)
  const pad={l:30,r:20,t:10,b:30}
  const W_=W-pad.l-pad.r,H_=H-pad.t-pad.b
  const toX=function(v){return pad.l+((v-mnX)/(Math.max(mxX-mnX,1)))*W_}
  const toY=function(v){return pad.t+H_-((v-mnY)/(Math.max(mxY-mnY,1)))*H_}
  ctx.fillStyle='rgba(255,255,255,0.35)'
  ctx.font='11px system-ui'
  ctx.fillText('logit_1 →',W_/2+pad.l-20,H-4)
  ctx.save()
  ctx.translate(12,H_/2+pad.t+20)
  ctx.rotate(-Math.PI/2)
  ctx.fillText('logit_2 →',0,0)
  ctx.restore()
  pts.forEach(function(_,i){
    const x=toX(xs[i]),y=toY(ys[i])
    const t=(objs[i]-mnO)/(Math.max(mxO-mnO,1))
    const r_=Math.round(99+(74-99)*t)
    const g_=Math.round(102+(222-102)*t)
    const b_=Math.round(241+(128-241)*t)
    const a=0.2+0.8*t
    ctx.beginPath()
    ctx.arc(x,y,5,0,Math.PI*2)
    ctx.fillStyle='rgba('+r_+','+g_+','+b_+','+a+')'
    ctx.fill()
  })
  const bestIdx=objs.indexOf(Math.max.apply(null,objs))
  ctx.beginPath()
  ctx.arc(toX(xs[bestIdx]),toY(ys[bestIdx]),8,0,Math.PI*2)
  ctx.strokeStyle='rgba(74,222,128,0.9)'
  ctx.lineWidth=2
  ctx.stroke()
}

document.addEventListener('DOMContentLoaded',function(){
  buildLayout()
  connectWS()
  startCFDPolling()
  loadDiagReport()
  fetch('/api/live').then(function(r){return r.json()}).then(function(d){
    if(d.job){S.job=d.job;renderOptimization();renderPipeline()}
    if(d.heartbeat){S.heartbeat=d.heartbeat;renderHeartbeat()}
  }).catch(function(){})
})
