"""Write an offline HTML player for simulator frames."""
import json


def write_replay(path, world, frames, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "bounds": world["bounds"],
        "prisms": [[p["kind"], p["ring"], p["holes"], p["top"]] for p in world["prisms"]],
        "frames": frames,
        "result": result,
    }, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Mech Royale · 对局回放</title>
<style>
:root{color-scheme:dark;font:15px system-ui,sans-serif;background:#0b171a;color:#e2efe9}
body{max-width:1120px;margin:32px auto;padding:0 20px}
h1{font-size:26px;margin:0 0 12px}header,section{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
canvas{display:block;width:100%;background:#102125;border:1px solid #254247;border-radius:12px;margin:12px 0}
input[type=range]{flex:1;min-width:140px;accent-color:#67ddb2} button,select{background:#20383d;color:inherit;border:1px solid #3f6566;border-radius:6px;padding:7px 12px}
p{color:#a4c2ba;margin:7px 0} strong{color:#67ddb2}.base{color:#ffb678}
</style>
<body><h1>Mech Royale 对局回放</h1>
<header><strong class="base">Practice 测试机</strong><span>灰色为模拟对手</span><span id="result"></span></header>
<canvas id="arena" width="1080" height="665" aria-label="校园地图上的机甲对局"></canvas>
<section><button id="play" type="button">播放</button><input id="clock" type="range" min="0" value="0" aria-label="回放进度">
<span id="time"></span><select id="rate" aria-label="播放速度"><option value="1">1×</option><option value="2" selected>2×</option><option value="4">4×</option><option value="8">8×</option></select></section>
<p id="status"></p>
<script id="data" type="application/json">__DATA__</script>
<script>
(() => {
const data=JSON.parse(document.querySelector('#data').textContent);
const canvas=document.querySelector('#arena'), ctx=canvas.getContext('2d');
const timeline=document.querySelector('#clock'), button=document.querySelector('#play'), speed=document.querySelector('#rate');
const {minX,minY,maxX,maxY}=data.bounds, width=canvas.width, height=canvas.height;
const scale=Math.min((width-52)/(maxX-minX),(height-52)/(maxY-minY));
const left=(width-(maxX-minX)*scale)/2, top=(height-(maxY-minY)*scale)/2;
const xy=(x,y)=>[left+(x-minX)*scale,top+(maxY-y)*scale];
const circle=(x,y,r)=>{ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2)};
let current=0, playing=false, offset=0, last=0;
timeline.max=Math.max(0,data.frames.length-1);
document.querySelector('#result').textContent=Object.entries(data.result).map(([n,s])=>n+' '+(s.place?'第 '+s.place+' 名':'存活至测试结束')+' · '+s.kills+' 击杀').join('　');
function polygon(ring){
 for(let i=0;i<ring.length;i+=2){const p=xy(ring[i],ring[i+1]); if(i===0)ctx.moveTo(...p);else ctx.lineTo(...p);}
 ctx.closePath();
}
function draw(){
 const f=data.frames[current];if(!f)return;
 ctx.clearRect(0,0,width,height);
 for(const [kind,ring,holes] of data.prisms){
  ctx.beginPath();polygon(ring);for(const hole of holes)polygon(hole);
  ctx.fillStyle=kind==='bridge'?'#49636c':kind==='stadium'?'#585365':'#32474c';
  ctx.fill('evenodd');ctx.strokeStyle='#5d7676';ctx.lineWidth=.5;ctx.stroke();
 }
 const [zx,zy,zr]=f.zone;circle(...xy(zx,zy),Math.max(0,zr*scale));ctx.strokeStyle='#50bdde';ctx.lineWidth=2;ctx.stroke();
 for(const point of f.shots){circle(...xy(...point),1.6);ctx.fillStyle='#ffd786';ctx.fill();}
 for(const path of f.beams){
  ctx.beginPath();path.forEach((p,i)=>{if(i===0)ctx.moveTo(...xy(...p));else ctx.lineTo(...xy(...p));});
  ctx.strokeStyle='#ff8771';ctx.lineWidth=2;ctx.stroke();
 }
 const stats=[];
 for(const [id,x,y,z,hp,kills,energy,agent] of f.mechs){
  const p=xy(x,y), player=id===0, c=player?'#ffb678':'#a0afb1';
  circle(...p,player?6.2:3.4);ctx.fillStyle=c;ctx.fill();
  ctx.font='13px system-ui';ctx.fillText(id+(player?' · '+Math.round(z)+'m':''),p[0]+8,p[1]+3);
  if(player)stats.push(agent+' HP '+hp+' · 能量 '+energy+' · 击杀 '+kills+' · Z '+z+'m');
 }
 document.querySelector('#time').textContent=f.t.toFixed(1)+' s';
 document.querySelector('#status').textContent=stats.join('　|　');
 timeline.value=current;
}
function cycle(stamp){
 if(playing){
  offset+=(stamp-last)*Number(speed.value)/200;
  if(offset>=1){current=Math.min(data.frames.length-1,current+Math.floor(offset));offset%=1;draw();}
  if(current>=data.frames.length-1){playing=false;button.textContent='播放';}
 }
 last=stamp;requestAnimationFrame(cycle);
}
timeline.addEventListener('input',()=>{current=Number(timeline.value);offset=0;draw();});
button.addEventListener('click',()=>{if(!playing&&current>=data.frames.length-1){current=0;draw();}playing=!playing;button.textContent=playing?'暂停':'播放';});
draw();requestAnimationFrame(cycle);
})();
</script></body></html>"""
    destination = path.with_suffix(".html")
    destination.write_text(page.replace("__DATA__", payload), encoding="utf-8")
    return destination
