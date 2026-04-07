import { useState, useEffect } from "react";

// ============ DATA ============
const STATES = ["RUN","RUN","RUN","RUN","RUN","RUN","RUN","RUN","IDLE","IDLE","STOP","STOP","MAINT"];
const WO = ["WO-2026-0341","WO-2026-0342","WO-2026-0338","WO-2026-0345","WO-2026-0350","WO-2026-0355","WO-2026-0360","WO-2026-0348"];
const BOARDS = ["PCB-A1020","PCB-B2150","PCB-C3080","PCB-D4010","PCB-E5060","PCB-F6020","PCB-G7090","PCB-H8010"];
const DS = [30,40,50,60,80];
const TARGET = 75;

function gen24h(state) {
  return Array.from({length:24},(_,h)=>{
    if (state==="STOP"||state==="MAINT") return {hour:h, util: Math.floor(Math.random()*10)};
    if (state==="IDLE") return {hour:h, util: h>=8&&h<20 ? 20+Math.floor(Math.random()*30) : Math.floor(Math.random()*15)};
    if (h>=8 && h<20) return {hour:h, util: 55+Math.floor(Math.random()*45)};
    if (h>=20 || h<2) return {hour:h, util: 30+Math.floor(Math.random()*50)};
    return {hour:h, util: 10+Math.floor(Math.random()*40)};
  });
}

const MACHINES = Array.from({length:13},(_,i)=>{
  const id=String(i+1).padStart(2,"0"), state=STATES[i];
  const dur = state==="RUN"?10+Math.floor(Math.random()*180): state==="IDLE"?3+Math.floor(Math.random()*45): 5+Math.floor(Math.random()*120);
  const uD = state==="RUN"?65+Math.floor(Math.random()*25): state==="IDLE"?30+Math.floor(Math.random()*20): Math.floor(Math.random()*15);
  const uW = Math.min(99,uD+Math.floor(Math.random()*10)-5);
  const uM = Math.min(99,uW+Math.floor(Math.random()*8)-3);
  const hD = state==="RUN"?8000+Math.floor(Math.random()*12000): state==="IDLE"?2000+Math.floor(Math.random()*3000): Math.floor(Math.random()*1000);
  const tH=400+Math.floor(Math.random()*800), dH=Math.floor(tH*(0.15+Math.random()*0.7));
  return {
    id:`DRILL-${id}`, state, dur, timeline: gen24h(state),
    util:{day:uD,week:uW,month:uM},
    holes:{day:hD,week:hD*6,month:hD*24},
    detail: state==="RUN"?{
      wo:WO[i%WO.length], board:BOARDS[i%BOARDS.length],
      panel:`${Math.floor(Math.random()*3)+1}/4`,
      drill:DS[i%DS.length], totalH:tH, doneH:dH,
      eta:5+Math.floor(Math.random()*40),
    }:null,
  };
});

const MONTH_NAMES = ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"];
const MONTH_DATA = MONTH_NAMES.map((name,i) => {
  const base = i<2 ? 55+Math.floor(Math.random()*15) : 60+Math.floor(Math.random()*20);
  const holes = 100000+Math.floor(Math.random()*200000);
  return { label: name, month: i+1, util: i<=2 ? base : null, holes: i<=2 ? holes : null, isCurrent: i===2 };
});
const WEEK_DATA_BY_MONTH = {};
for (let m=1;m<=3;m++){
  WEEK_DATA_BY_MONTH[m] = Array.from({length: m===2?4:m===3?4:5},(_,w)=>({
    label:`W${w+1}`, week:w+1, util:50+Math.floor(Math.random()*35),
    holes:20000+Math.floor(Math.random()*60000), isCurrent:m===3&&w===3
  }));
}
const DAY_DATA_BY_WEEK = {};
for (let m=1;m<=3;m++){
  DAY_DATA_BY_WEEK[m]={};
  const wc = m===2?4:m===3?4:5;
  for(let w=1;w<=wc;w++){
    DAY_DATA_BY_WEEK[m][w]=["一","二","三","四","五","六","日"].map((d,di)=>({
      label:d, util:di>=5?20+Math.floor(Math.random()*30):50+Math.floor(Math.random()*40),
      holes:5000+Math.floor(Math.random()*15000), isCurrent:m===3&&w===4&&di===3
    }));
  }
}

const SC = {
  RUN:  {label:"稼動中",bg:"#059669"},
  IDLE: {label:"閒置",  bg:"#d97706"},
  STOP: {label:"停機",  bg:"#dc2626"},
  MAINT:{label:"維護中",bg:"#7c3aed"},
};
const fmt=n=>n?n.toLocaleString():"—";
const durStr=m=>m>=60?`${Math.floor(m/60)}h ${m%60}m`:`${m} min`;

// ============ HEADER ============
function Header({tab,setTab,countdown}){
  const cm=Math.floor(countdown/60), cs=String(countdown%60).padStart(2,"0");
  const tabs=[
    {key:"overview", label:"機台總覽"},
    {key:"ranking",  label:"稼動排行"},
    {key:"analysis", label:"稼動分析"},
    {key:"detail",   label:"作業細節"},
  ];
  return (
    <div style={{background:"#fff",borderBottom:"1px solid #e5e7eb",padding:"0 28px",position:"sticky",top:0,zIndex:10}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"14px 0 0"}}>
        <div style={{display:"flex",alignItems:"center",gap:12}}>
          <div style={{width:6,height:28,borderRadius:3,background:"linear-gradient(180deg,#059669,#3b82f6)"}}/>
          <div>
            <h1 style={{margin:0,fontSize:20,fontWeight:700,color:"#111827"}}>鑽孔機稼動監控</h1>
            <p style={{margin:0,fontSize:11,color:"#9ca3af"}}>13 台 Takeuchi ｜ 每 10 分鐘同步</p>
          </div>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:6,padding:"4px 10px",background:"#f0fdf4",borderRadius:6,border:"1px solid #bbf7d0"}}>
          <div style={{width:6,height:6,borderRadius:"50%",background:"#22c55e",animation:"pulse 2s infinite"}}/>
          <span style={{fontSize:11,color:"#059669",fontWeight:600,fontVariantNumeric:"tabular-nums"}}>{cm}:{cs} 後更新</span>
        </div>
      </div>
      <div style={{display:"flex",gap:0,marginTop:12}}>
        {tabs.map(t=>(
          <button key={t.key} onClick={()=>setTab(t.key)} style={{
            padding:"8px 20px 10px",border:"none",background:"transparent",cursor:"pointer",
            borderBottom: tab===t.key?"2.5px solid #111827":"2.5px solid transparent",
            color: tab===t.key?"#111827":"#9ca3af",
            fontWeight: tab===t.key?700:500, fontSize:14, transition:"all 0.15s",
          }}>{t.label}</button>
        ))}
      </div>
    </div>
  );
}

// ============ TAB 1: OVERVIEW ============
function OverviewTab(){
  const run=MACHINES.filter(m=>m.state==="RUN").length;
  const idle=MACHINES.filter(m=>m.state==="IDLE").length;
  const stop=MACHINES.filter(m=>m.state==="STOP"||m.state==="MAINT").length;
  return (
    <div>
      <div style={{display:"flex",gap:12,marginBottom:20}}>
        {[
          {label:"稼動中",val:run,unit:"台",color:"#059669"},
          {label:"閒置",val:idle,unit:"台",color:"#d97706"},
          {label:"停機 / 維護",val:stop,unit:"台",color:"#dc2626"},
        ].map((c,i)=>(
          <div key={i} style={{flex:1,background:"#fff",borderRadius:10,padding:"14px 18px",border:"1px solid #e5e7eb",borderLeft:`4px solid ${c.color}`}}>
            <div style={{fontSize:11,color:"#6b7280",marginBottom:2}}>{c.label}</div>
            <div style={{display:"flex",alignItems:"baseline",gap:4}}>
              <span style={{fontSize:32,fontWeight:800,color:c.color,lineHeight:1}}>{c.val}</span>
              <span style={{fontSize:13,color:"#9ca3af"}}>{c.unit}</span>
            </div>
          </div>
        ))}
      </div>
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill, minmax(220px, 1fr))",gap:10}}>
        {MACHINES.map(m=>{
          const s=SC[m.state];
          return (
            <div key={m.id} style={{background:"#fff",borderRadius:10,overflow:"hidden",border:"1px solid #e5e7eb"}}>
              <div style={{background:s.bg,color:"#fff",padding:"10px 14px",display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                <span style={{fontSize:15,fontWeight:800}}>{m.id}</span>
                <span style={{fontSize:12,fontWeight:600,background:"rgba(255,255,255,0.2)",padding:"2px 10px",borderRadius:20}}>{s.label}</span>
              </div>
              <div style={{padding:"10px 14px",display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                <div>
                  <div style={{fontSize:11,color:"#9ca3af"}}>持續時間</div>
                  <div style={{fontSize:18,fontWeight:700,color:"#111827"}}>{durStr(m.dur)}</div>
                </div>
                {m.state==="IDLE"&&m.dur>30&&(
                  <div style={{fontSize:11,fontWeight:600,color:"#dc2626",background:"#fef2f2",padding:"3px 8px",borderRadius:4}}>⚠ 閒置過久</div>
                )}
                {m.state==="RUN"&&m.detail&&(
                  <div style={{textAlign:"right"}}>
                    <div style={{fontSize:10,color:"#9ca3af"}}>工號</div>
                    <div style={{fontSize:12,fontWeight:600,color:"#374151"}}>{m.detail.wo}</div>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
      <div style={{display:"flex",gap:16,marginTop:16,fontSize:12,color:"#6b7280"}}>
        {Object.entries(SC).map(([k,v])=>(
          <div key={k} style={{display:"flex",alignItems:"center",gap:5}}>
            <div style={{width:14,height:8,borderRadius:2,background:v.bg}}/><span>{v.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ============ LARGE TREND BAR CHART ============
function TrendChart({ data, onBarClick, activeIndex }) {
  const chartH = 340;
  const maxVal = 100;
  const barCount = data.length;
  const barW = barCount > 10 ? 56 : barCount > 7 ? 72 : 90;
  const gap = barCount > 10 ? 8 : 14;

  return (
    <div style={{overflowX:"auto",padding:"10px 0"}}>
      <div style={{position:"relative", paddingLeft:44, paddingBottom:50, height: chartH+50, minWidth: barCount*(barW+gap)+80}}>

        {/* Y-axis grid */}
        {[0,25,50,75,100].map(v=>{
          const y = chartH - (v/maxVal)*chartH;
          const isTarget = v===TARGET;
          return (
            <div key={v} style={{position:"absolute",left:0,top:y,width:"100%",display:"flex",alignItems:"center"}}>
              <span style={{
                fontSize:12,color:isTarget?"#3b82f6":"#9ca3af",width:36,textAlign:"right",marginRight:8,
                fontWeight:isTarget?700:400,fontVariantNumeric:"tabular-nums",
              }}>{v}%</span>
              <div style={{flex:1,height:isTarget?2:1,background:isTarget?"#3b82f6":"#e5e7eb",opacity:isTarget?0.4:0.6,
                borderStyle:isTarget?"dashed":"solid",borderWidth:0,
              }}/>
            </div>
          );
        })}

        {/* Target label */}
        <div style={{position:"absolute",right:8,top:chartH-(TARGET/maxVal)*chartH-10,fontSize:11,color:"#3b82f6",fontWeight:700,background:"#eff6ff",padding:"2px 8px",borderRadius:4}}>
          目標 {TARGET}%
        </div>

        {/* Bars */}
        <div style={{position:"absolute",left:52,bottom:50,display:"flex",gap,alignItems:"flex-end",height:chartH}}>
          {data.map((d,i)=>{
            const val = d.util;
            const hasData = val !== null && val !== undefined;
            const h = hasData ? (val/maxVal)*chartH : 0;
            const c = !hasData ? "#e5e7eb" : val>=TARGET ? "#059669" : val>=50 ? "#d97706" : "#dc2626";
            const isActive = activeIndex === i;
            const isClickable = hasData && onBarClick;
            return (
              <div key={i} style={{display:"flex",flexDirection:"column",alignItems:"center",gap:4,cursor:isClickable?"pointer":"default"}}
                onClick={()=>isClickable && onBarClick(i,d)}>
                {/* Value on top */}
                {hasData && (
                  <span style={{fontSize:16,fontWeight:800,color:c,fontVariantNumeric:"tabular-nums"}}>
                    {val}%
                  </span>
                )}
                {!hasData && (
                  <span style={{fontSize:12,color:"#d1d5db"}}>—</span>
                )}
                {/* Bar */}
                <div style={{
                  width:barW, height: hasData ? Math.max(h,6) : chartH*0.03,
                  background: hasData ? c : "#f3f4f6",
                  borderRadius:"6px 6px 0 0",
                  transition:"all 0.3s ease",
                  opacity: hasData ? (isActive?1:0.8) : 0.25,
                  border: isActive ? "3px solid #111827" : "2px solid transparent",
                  boxShadow: isActive ? "0 -4px 12px rgba(0,0,0,0.12)" : isClickable&&hasData ? "0 -1px 4px rgba(0,0,0,0.04)" : "none",
                  position:"relative",
                }}>
                  {d.isCurrent && hasData && (
                    <div style={{position:"absolute",top:-5,left:"50%",transform:"translateX(-50%)",width:8,height:8,borderRadius:4,background:"#3b82f6",border:"2px solid #fff"}}/>
                  )}
                </div>
                {/* X label */}
                <span style={{
                  fontSize:14,fontWeight:isActive||d.isCurrent?800:500,
                  color:isActive?"#111827":d.isCurrent?"#3b82f6":"#6b7280",
                  marginTop:4,
                }}>
                  {d.label}
                </span>
                {/* Holes */}
                {hasData && (
                  <span style={{fontSize:10,color:"#9ca3af",marginTop:-2}}>{fmt(d.holes)} 孔</span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ============ TAB 2: RANKING ============
function RankingTab(){
  const [drillLevel,setDrillLevel]=useState("month");
  const [selectedMonth,setSelectedMonth]=useState(null);
  const [selectedWeek,setSelectedWeek]=useState(null);
  const [activeBarIdx,setActiveBarIdx]=useState(null);

  let chartData=[], chartTitle="", breadcrumb=[];

  if(drillLevel==="month"){
    chartData = MONTH_DATA;
    chartTitle = "2026 年 — 各月平均稼動率";
    breadcrumb = [{label:"年度總覽",active:true}];
  } else if(drillLevel==="week" && selectedMonth){
    chartData = WEEK_DATA_BY_MONTH[selectedMonth] || [];
    chartTitle = `2026 年 ${selectedMonth} 月 — 各週平均稼動率`;
    breadcrumb = [{label:"年度總覽",active:false,action:()=>{setDrillLevel("month");setSelectedMonth(null);setSelectedWeek(null);setActiveBarIdx(null);}},{label:`${selectedMonth}月`,active:true}];
  } else if(drillLevel==="day" && selectedMonth && selectedWeek){
    chartData = (DAY_DATA_BY_WEEK[selectedMonth]||{})[selectedWeek] || [];
    chartTitle = `2026 年 ${selectedMonth} 月 第${selectedWeek}週 — 各日平均稼動率`;
    breadcrumb = [
      {label:"年度總覽",active:false,action:()=>{setDrillLevel("month");setSelectedMonth(null);setSelectedWeek(null);setActiveBarIdx(null);}},
      {label:`${selectedMonth}月`,active:false,action:()=>{setDrillLevel("week");setSelectedWeek(null);setActiveBarIdx(null);}},
      {label:`第${selectedWeek}週`,active:true},
    ];
  }

  const handleBarClick = (idx, d) => {
    setActiveBarIdx(idx);
    if(drillLevel==="month" && d.util!==null){
      setSelectedMonth(d.month);
      setDrillLevel("week");
      setActiveBarIdx(null);
    } else if(drillLevel==="week"){
      setSelectedWeek(d.week);
      setDrillLevel("day");
      setActiveBarIdx(null);
    }
  };

  const currentData = chartData.filter(d=>d.util!==null);
  const avgUtil = currentData.length ? Math.round(currentData.reduce((s,d)=>s+d.util,0)/currentData.length) : 0;
  const maxUtil = currentData.length ? Math.max(...currentData.map(d=>d.util)) : 0;
  const minUtil = currentData.length ? Math.min(...currentData.map(d=>d.util)) : 0;
  const maxLabel = currentData.find(d=>d.util===maxUtil)?.label||"";
  const minLabel = currentData.find(d=>d.util===minUtil)?.label||"";

  return (
    <div>
      {/* Summary Row */}
      <div style={{display:"flex",gap:12,marginBottom:20}}>
        <div style={{flex:2,background:"#fff",borderRadius:12,padding:"20px 28px",border:"1px solid #e5e7eb"}}>
          <div style={{fontSize:12,color:"#6b7280",marginBottom:6}}>期間平均稼動率（13 台）</div>
          <div style={{display:"flex",alignItems:"baseline",gap:6}}>
            <span style={{fontSize:64,fontWeight:800,color:avgUtil>=TARGET?"#059669":"#d97706",lineHeight:1,fontVariantNumeric:"tabular-nums",letterSpacing:"-2px"}}>{avgUtil}</span>
            <span style={{fontSize:28,fontWeight:700,color:avgUtil>=TARGET?"#059669":"#d97706"}}>%</span>
          </div>
          <div style={{fontSize:13,color:avgUtil>=TARGET?"#059669":"#d97706",marginTop:6,fontWeight:600}}>
            {avgUtil>=TARGET?"✓ 達標":"✗ 未達標"}
            <span style={{color:"#9ca3af",fontWeight:400,marginLeft:8}}>目標 {TARGET}%</span>
          </div>
        </div>
        {[
          {label:"最高",val:`${maxUtil}%`,sub:maxLabel,color:"#059669"},
          {label:"最低",val:`${minUtil}%`,sub:minLabel,color:"#dc2626"},
          {label:"波動",val:`${maxUtil-minUtil}%`,sub:maxUtil-minUtil>20?"波動大":"穩定",color:maxUtil-minUtil>20?"#d97706":"#059669"},
        ].map((c,i)=>(
          <div key={i} style={{flex:1,background:"#fff",borderRadius:12,padding:"20px 24px",border:"1px solid #e5e7eb",display:"flex",flexDirection:"column",justifyContent:"center"}}>
            <div style={{fontSize:12,color:"#6b7280",marginBottom:4}}>{c.label}</div>
            <div style={{fontSize:32,fontWeight:800,color:c.color,lineHeight:1,fontVariantNumeric:"tabular-nums"}}>{c.val}</div>
            <div style={{fontSize:12,color:"#9ca3af",marginTop:4}}>{c.sub}</div>
          </div>
        ))}
      </div>

      {/* Chart Area */}
      <div style={{background:"#fff",borderRadius:12,border:"1px solid #e5e7eb",padding:"24px 28px"}}>
        {/* Breadcrumb + Title */}
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:8}}>
          <div>
            <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:8}}>
              {breadcrumb.map((b,i)=>(
                <div key={i} style={{display:"flex",alignItems:"center",gap:6}}>
                  {i>0 && <span style={{color:"#d1d5db",fontSize:13}}>›</span>}
                  {b.active ? (
                    <span style={{fontSize:13,fontWeight:700,color:"#111827",background:"#f3f4f6",padding:"3px 10px",borderRadius:4}}>{b.label}</span>
                  ) : (
                    <button onClick={b.action} style={{
                      fontSize:13,fontWeight:500,color:"#3b82f6",background:"#eff6ff",border:"none",
                      cursor:"pointer",padding:"3px 10px",borderRadius:4,
                    }}>{b.label}</button>
                  )}
                </div>
              ))}
            </div>
            <div style={{fontSize:18,fontWeight:700,color:"#111827"}}>{chartTitle}</div>
          </div>
          {drillLevel!=="day" && (
            <div style={{fontSize:12,color:"#6b7280",background:"#f3f4f6",padding:"6px 14px",borderRadius:6,fontWeight:500}}>
              👆 點擊柱狀下鑽
            </div>
          )}
        </div>

        <TrendChart data={chartData} onBarClick={drillLevel!=="day"?handleBarClick:null} activeIndex={activeBarIdx} />
      </div>
    </div>
  );
}

// ============ MACHINE RANKING (shared component) ============
function MachineRanking(){
  const sorted=[...MACHINES].sort((a,b)=>b.util.month-a.util.month);
  return (
    <div style={{background:"#fff",borderRadius:10,border:"1px solid #e5e7eb",padding:"18px 24px"}}>
      <div style={{fontSize:13,fontWeight:600,color:"#374151",marginBottom:12}}>各機台稼動率排行（本月）</div>
      <div style={{display:"flex",flexDirection:"column",gap:6}}>
        {sorted.map((m,i)=>{
          const v=m.util.month;
          const c=v>=TARGET?"#059669":v>=50?"#d97706":"#dc2626";
          const s=SC[m.state];
          return (
            <div key={m.id} style={{display:"flex",alignItems:"center",gap:10,height:28}}>
              <span style={{
                width:20,height:20,borderRadius:10,display:"flex",alignItems:"center",justifyContent:"center",
                fontSize:10,fontWeight:700,flexShrink:0,
                background:i<3?"#f0fdf4":i>=10?"#fef2f2":"#f9fafb",
                color:i<3?"#059669":i>=10?"#dc2626":"#9ca3af",
              }}>{i+1}</span>
              <div style={{width:72,display:"flex",alignItems:"center",gap:5,flexShrink:0}}>
                <div style={{width:7,height:7,borderRadius:2,background:s.bg}}/>
                <span style={{fontSize:12,fontWeight:600,color:"#374151"}}>{m.id.replace("DRILL-","機 ")}</span>
              </div>
              <div style={{flex:1,height:16,background:"#f3f4f6",borderRadius:4,overflow:"hidden",position:"relative"}}>
                <div style={{width:`${v}%`,height:"100%",background:c,borderRadius:4,transition:"width 0.5s",display:"flex",alignItems:"center",justifyContent:"flex-end",paddingRight:6}}>
                  {v>18&&<span style={{fontSize:10,fontWeight:700,color:"#fff"}}>{v}%</span>}
                </div>
                <div style={{position:"absolute",left:`${TARGET}%`,top:0,bottom:0,width:1.5,background:"#3b82f6",opacity:0.5}}/>
              </div>
              {v<=18&&<span style={{fontSize:11,fontWeight:700,color:c,width:30}}>{v}%</span>}
              <span style={{fontSize:11,color:"#6b7280",width:72,textAlign:"right",flexShrink:0}}>{fmt(m.holes.month)} 孔</span>
            </div>
          );
        })}
      </div>
      <div style={{display:"flex",alignItems:"center",gap:6,marginTop:10,fontSize:11,color:"#9ca3af"}}>
        <div style={{width:12,height:2,background:"#3b82f6",borderRadius:1}}/><span>目標 {TARGET}%</span>
      </div>
    </div>
  );
}

// ============ TAB 3: ANALYSIS ============
function AnalysisTab(){
  const [hoveredCell,setHoveredCell]=useState(null);
  const [filter,setFilter]=useState("all");
  const dayStart=8,dayEnd=20;
  const filterOpts=[
    {key:"all",label:"全部顯示",desc:"完整24小時稼動分布"},
    {key:"low25",label:"< 25%",desc:"嚴重低稼動時段"},
    {key:"low50",label:"< 50%",desc:"低於半數稼動時段"},
    {key:"high75",label:"≥ 75%",desc:"達標時段"},
  ];
  const utilColor=(v,f)=>{
    if(f==="low25") return v<25?"#dc2626":"#f9fafb";
    if(f==="low50") return v<50?(v<25?"#dc2626":"#f59e0b"):"#f9fafb";
    if(f==="high75") return v>=75?"#059669":"#f9fafb";
    if(v===0) return "#f3f4f6"; if(v<25) return "#fecaca"; if(v<50) return "#fde68a"; if(v<75) return "#bef264"; return "#4ade80";
  };
  const textColor=(v,f)=>{
    if(f==="low25") return v<25?"#fff":"#d1d5db";
    if(f==="low50") return v<50?"#fff":"#d1d5db";
    if(f==="high75") return v>=75?"#fff":"#d1d5db";
    if(v>=75) return "#166534"; if(v>=50) return "#713f12"; if(v>=25) return "#991b1b"; return "#9ca3af";
  };
  const shiftAvgs=MACHINES.map(m=>{
    const day=m.timeline.filter(t=>t.hour>=dayStart&&t.hour<dayEnd);
    const night=m.timeline.filter(t=>t.hour<dayStart||t.hour>=dayEnd);
    return {day:day.length?Math.round(day.reduce((s,t)=>s+t.util,0)/day.length):0,night:night.length?Math.round(night.reduce((s,t)=>s+t.util,0)/night.length):0};
  });
  const oDay=Math.round(shiftAvgs.reduce((s,a)=>s+a.day,0)/shiftAvgs.length);
  const oNight=Math.round(shiftAvgs.reduce((s,a)=>s+a.night,0)/shiftAvgs.length);
  const countFiltered=()=>{let c=0;MACHINES.forEach(m=>m.timeline.forEach(t=>{if(filter==="low25"&&t.util<25)c++;if(filter==="low50"&&t.util<50)c++;if(filter==="high75"&&t.util>=75)c++;}));return c;};
  const cellW=32,cellH=28,labelW=72,shiftW=52;

  return (
    <div>
      {/* Machine Ranking - moved here */}
      <MachineRanking />

      <div style={{height:20}} />

      {/* Shift Summary */}
      <div style={{display:"flex",gap:12,marginBottom:16}}>
        {[
          {label:"日班平均（08–20）",val:`${oDay}%`,color:oDay>=TARGET?"#059669":"#d97706",bg:"#f0fdf4",border:"#bbf7d0"},
          {label:"夜班平均（20–08）",val:`${oNight}%`,color:oNight>=TARGET?"#059669":"#dc2626",bg:oNight>=TARGET?"#f0fdf4":"#fef2f2",border:oNight>=TARGET?"#bbf7d0":"#fecaca"},
          {label:"日夜落差",val:`${oDay-oNight}%`,color:"#dc2626",bg:"#fef2f2",border:"#fecaca"},
        ].map((c,i)=>(
          <div key={i} style={{flex:1,background:c.bg,borderRadius:10,padding:"12px 18px",border:`1px solid ${c.border}`}}>
            <div style={{fontSize:11,color:"#6b7280",marginBottom:2}}>{c.label}</div>
            <div style={{fontSize:28,fontWeight:800,color:c.color,lineHeight:1}}>{c.val}</div>
          </div>
        ))}
      </div>

      {/* Heatmap */}
      <div style={{background:"#fff",borderRadius:10,border:"1px solid #e5e7eb",padding:"16px 20px"}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:14}}>
          <div style={{fontSize:13,fontWeight:600,color:"#374151"}}>24 小時稼動熱力圖</div>
          <div style={{display:"flex",gap:4}}>
            {filterOpts.map(f=>(
              <button key={f.key} onClick={()=>setFilter(f.key)} style={{
                padding:"5px 14px",borderRadius:6,border:"1.5px solid",fontSize:12,fontWeight:600,cursor:"pointer",transition:"all 0.15s",
                background:filter===f.key?(f.key==="low25"?"#dc2626":f.key==="low50"?"#d97706":f.key==="high75"?"#059669":"#111827"):"#fff",
                color:filter===f.key?"#fff":"#6b7280",borderColor:filter===f.key?"transparent":"#e5e7eb",
              }}>{f.label}</button>
            ))}
          </div>
        </div>
        {filter!=="all"&&(
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"8px 14px",borderRadius:6,marginBottom:12,fontSize:12,
            background:filter==="low25"?"#fef2f2":filter==="low50"?"#fffbeb":"#f0fdf4",
            border:`1px solid ${filter==="low25"?"#fecaca":filter==="low50"?"#fde68a":"#bbf7d0"}`,
            color:filter==="low25"?"#dc2626":filter==="low50"?"#d97706":"#059669",
          }}>
            <span style={{fontWeight:500}}>{filterOpts.find(f=>f.key===filter)?.desc} — 其餘時段淡化處理</span>
            <span style={{fontWeight:700}}>共 {countFiltered()} 個時段</span>
          </div>
        )}
        <div style={{overflowX:"auto"}}>
          <div style={{minWidth:labelW+cellW*24+shiftW*2+16}}>
            <div style={{display:"flex",alignItems:"flex-end",marginBottom:2,paddingLeft:labelW}}>
              {Array.from({length:24},(_,h)=>(
                <div key={h} style={{width:cellW,textAlign:"center",fontSize:10,fontWeight:h===dayStart||h===dayEnd?700:400,color:h===dayStart||h===dayEnd?"#3b82f6":"#9ca3af"}}>
                  {h.toString().padStart(2,"0")}
                </div>
              ))}
              <div style={{width:shiftW,textAlign:"center",fontSize:10,fontWeight:600,color:"#059669",marginLeft:8}}>日班</div>
              <div style={{width:shiftW,textAlign:"center",fontSize:10,fontWeight:600,color:"#6366f1"}}>夜班</div>
            </div>
            <div style={{display:"flex",marginBottom:4,paddingLeft:labelW}}>
              {Array.from({length:24},(_,h)=>(
                <div key={h} style={{width:cellW,height:3,background:(h>=dayStart&&h<dayEnd)?"#d1fae5":"#e0e7ff"}}/>
              ))}
            </div>
            {MACHINES.map((m,mi)=>{
              const s=SC[m.state]; const sa=shiftAvgs[mi];
              return (
                <div key={m.id} style={{display:"flex",alignItems:"center",marginBottom:2}}>
                  <div style={{width:labelW,display:"flex",alignItems:"center",gap:5,flexShrink:0}}>
                    <div style={{width:8,height:8,borderRadius:2,background:s.bg,flexShrink:0}}/>
                    <span style={{fontSize:11,fontWeight:600,color:"#374151"}}>{m.id.replace("DRILL-","機 ")}</span>
                  </div>
                  {m.timeline.map((t,hi)=>{
                    const isH=hoveredCell&&hoveredCell.mi===mi&&hoveredCell.hi===hi;
                    const bg=utilColor(t.util,filter); const tc=textColor(t.util,filter);
                    const isShiftB=t.hour===dayStart||t.hour===dayEnd;
                    const isActive=filter==="all"||(filter==="low25"&&t.util<25)||(filter==="low50"&&t.util<50)||(filter==="high75"&&t.util>=75);
                    return (
                      <div key={hi} onMouseEnter={()=>setHoveredCell({mi,hi,id:m.id,hour:t.hour,util:t.util})} onMouseLeave={()=>setHoveredCell(null)}
                        style={{width:cellW,height:cellH,background:bg,borderLeft:isShiftB?"2px solid #3b82f6":"1px solid rgba(255,255,255,0.8)",borderTop:"1px solid rgba(255,255,255,0.8)",
                          display:"flex",alignItems:"center",justifyContent:"center",fontSize:9,fontWeight:isActive?700:400,color:tc,cursor:"default",
                          transform:isH?"scale(1.15)":"scale(1)",transition:"all 0.1s",zIndex:isH?5:1,position:"relative",borderRadius:isH?3:0,
                          boxShadow:isH?"0 2px 8px rgba(0,0,0,0.2)":"none",
                        }}>
                        {(isActive&&t.util>0)||isH?t.util:""}
                      </div>
                    );
                  })}
                  <div style={{width:shiftW,marginLeft:8,textAlign:"center",fontSize:11,fontWeight:700,color:sa.day>=TARGET?"#059669":"#d97706",background:sa.day>=TARGET?"#f0fdf4":"#fffbeb",borderRadius:4,padding:"4px 0"}}>{sa.day}%</div>
                  <div style={{width:shiftW,textAlign:"center",fontSize:11,fontWeight:700,color:sa.night>=TARGET?"#059669":sa.night>=40?"#d97706":"#dc2626",background:sa.night>=TARGET?"#f0fdf4":sa.night>=40?"#fffbeb":"#fef2f2",borderRadius:4,padding:"4px 0"}}>{sa.night}%</div>
                </div>
              );
            })}
            {hoveredCell&&(
              <div style={{position:"fixed",top:8,right:28,zIndex:100,background:"#111827",color:"#fff",padding:"8px 14px",borderRadius:8,fontSize:12,fontWeight:500,boxShadow:"0 4px 12px rgba(0,0,0,0.15)",display:"flex",gap:12}}>
                <span style={{fontWeight:700}}>{hoveredCell.id}</span>
                <span>{String(hoveredCell.hour).padStart(2,"0")}:00</span>
                <span style={{fontWeight:700,color:hoveredCell.util>=TARGET?"#4ade80":hoveredCell.util>=50?"#fbbf24":"#f87171"}}>{hoveredCell.util}%</span>
                <span style={{color:"#9ca3af"}}>{hoveredCell.hour>=8&&hoveredCell.hour<20?"日班":"夜班"}</span>
              </div>
            )}
          </div>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:12,marginTop:14,fontSize:11,color:"#6b7280",flexWrap:"wrap"}}>
          {filter==="all"?(
            <>
              <span>稼動率：</span>
              {[{label:"0%",color:"#f3f4f6"},{label:"<25%",color:"#fecaca"},{label:"25-49%",color:"#fde68a"},{label:"50-74%",color:"#bef264"},{label:"≥75%",color:"#4ade80"}].map((c,i)=>(
                <div key={i} style={{display:"flex",alignItems:"center",gap:3}}>
                  <div style={{width:14,height:10,borderRadius:2,background:c.color,border:"1px solid #e5e7eb"}}/><span>{c.label}</span>
                </div>
              ))}
            </>
          ):(
            <>
              <span>篩選模式：</span>
              <div style={{display:"flex",alignItems:"center",gap:3}}>
                <div style={{width:14,height:10,borderRadius:2,background:filter==="low25"?"#dc2626":filter==="low50"?"#f59e0b":"#059669"}}/><span>符合條件</span>
              </div>
              <div style={{display:"flex",alignItems:"center",gap:3}}>
                <div style={{width:14,height:10,borderRadius:2,background:"#f9fafb",border:"1px solid #e5e7eb"}}/><span>不符合（淡化）</span>
              </div>
            </>
          )}
          <span style={{marginLeft:8}}>｜</span>
          <div style={{display:"flex",alignItems:"center",gap:3}}>
            <div style={{width:2,height:10,background:"#3b82f6"}}/><span>班次分界</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ============ TAB 4: DETAIL ============
function DetailTab(){
  const running=MACHINES.filter(m=>m.state==="RUN"&&m.detail);
  const notRunning=MACHINES.filter(m=>m.state!=="RUN");
  return (
    <div>
      <div style={{fontSize:13,fontWeight:600,color:"#374151",marginBottom:10}}>稼動中（{running.length} 台）</div>
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill, minmax(340px, 1fr))",gap:12,marginBottom:24}}>
        {running.map(m=>{
          const d=m.detail; const pct=Math.round(d.doneH/d.totalH*100);
          return (
            <div key={m.id} style={{background:"#fff",borderRadius:10,border:"1px solid #e5e7eb",overflow:"hidden"}}>
              <div style={{background:"#059669",color:"#fff",padding:"8px 14px",display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                <span style={{fontSize:15,fontWeight:800}}>{m.id}</span>
                <span style={{fontSize:12,fontWeight:600}}>{durStr(m.dur)}</span>
              </div>
              <div style={{padding:"12px 14px"}}>
                <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:"8px 20px",fontSize:13,marginBottom:12}}>
                  {[["工號",d.wo,"#111827"],["板號",d.board,"#111827"],["針型",`${d.drill} 針`,"#059669"],["進度",`第 ${d.panel} 片`,"#111827"]].map(([k,v,c])=>(
                    <div key={k}>
                      <div style={{fontSize:10,color:"#9ca3af",marginBottom:1}}>{k}</div>
                      <div style={{fontWeight:700,color:c}}>{v}</div>
                    </div>
                  ))}
                </div>
                <div style={{marginBottom:10}}>
                  <div style={{display:"flex",justifyContent:"space-between",fontSize:11,marginBottom:4}}>
                    <span style={{color:"#6b7280"}}>孔數進度</span>
                    <span style={{fontWeight:700,color:"#374151"}}>{fmt(d.doneH)} / {fmt(d.totalH)}</span>
                  </div>
                  <div style={{height:10,background:"#f3f4f6",borderRadius:5,overflow:"hidden"}}>
                    <div style={{width:`${pct}%`,height:"100%",borderRadius:5,background:"linear-gradient(90deg,#059669,#10b981)",transition:"width 0.5s"}}/>
                  </div>
                  <div style={{textAlign:"right",fontSize:11,fontWeight:600,color:"#059669",marginTop:2}}>{pct}%</div>
                </div>
                <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"8px 12px",borderRadius:8,background:"#eff6ff",border:"1px solid #bfdbfe"}}>
                  <span style={{fontSize:12,color:"#3b82f6",fontWeight:500}}>⏱ 預計剩餘</span>
                  <span style={{fontSize:18,fontWeight:800,color:"#1d4ed8",fontVariantNumeric:"tabular-nums"}}>{d.eta} 分鐘</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <div style={{fontSize:13,fontWeight:600,color:"#9ca3af",marginBottom:8}}>未稼動（{notRunning.length} 台）</div>
      <div style={{display:"flex",flexWrap:"wrap",gap:8}}>
        {notRunning.map(m=>{
          const s=SC[m.state];
          return (
            <div key={m.id} style={{display:"flex",alignItems:"center",gap:8,background:"#fff",borderRadius:8,padding:"8px 14px",border:"1px solid #e5e7eb"}}>
              <div style={{width:10,height:10,borderRadius:2,background:s.bg}}/>
              <span style={{fontSize:13,fontWeight:700,color:"#374151"}}>{m.id}</span>
              <span style={{fontSize:12,color:s.bg,fontWeight:600}}>{s.label}</span>
              <span style={{fontSize:11,color:"#9ca3af"}}>{durStr(m.dur)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ============ MAIN ============
export default function Dashboard(){
  const [tab,setTab]=useState("overview");
  const [cd,setCd]=useState(600);
  useEffect(()=>{const t=setInterval(()=>setCd(p=>p<=0?600:p-1),1000);return()=>clearInterval(t);},[]);
  return (
    <div style={{fontFamily:"'Noto Sans TC','Helvetica Neue',sans-serif",background:"#f6f7f9",minHeight:"100vh",color:"#1a1a2e"}}>
      <Header tab={tab} setTab={setTab} countdown={cd}/>
      <div style={{padding:"20px 28px",maxWidth:1320,margin:"0 auto"}}>
        {tab==="overview"&&<OverviewTab/>}
        {tab==="ranking"&&<RankingTab/>}
        {tab==="analysis"&&<AnalysisTab/>}
        {tab==="detail"&&<DetailTab/>}
      </div>
      <style>{`@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}`}</style>
    </div>
  );
}
