import time,secrets
from sqlalchemy import select,func
from fastapi import HTTPException
from .models import Clock,ColdRoom,ColdPlan,ColdPlanLine,Consignment,VaseSample,AuctionLot
from .common import lock,pack,audit,stamp
from .grading import lifetime_pair
RANK={'A':0,'B':1,'C':2}


def confirmed_usage(s,exclude_plan=None):
    q=select(func.coalesce(func.sum(ColdPlanLine.crates),0)).join(ColdPlan,ColdPlan.id==ColdPlanLine.plan_id).where(ColdPlan.state=='confirmed')
    if exclude_plan is not None:q=q.where(ColdPlan.id!=exclude_plan)
    return int(s.scalar(q)or 0)


def plan_usage_by_consignment(s,exclude_plan=None):
    q=select(ColdPlanLine.consignment_id,func.sum(ColdPlanLine.crates)).join(ColdPlan,ColdPlan.id==ColdPlanLine.plan_id).where(ColdPlan.state=='confirmed')
    if exclude_plan is not None:q=q.where(ColdPlan.id!=exclude_plan)
    return {cid:int(total) for cid,total in s.execute(q.group_by(ColdPlanLine.consignment_id))}


def lot_usage(s,consignment_id):
    return int(s.scalar(select(func.coalesce(func.sum(AuctionLot.crates),0)).where(AuctionLot.consignment_id==consignment_id,AuctionLot.state!='cancelled'))or 0)


def confirmed_crates_for(s,consignment_id,exclude_plan=None):
    q=select(func.coalesce(func.sum(ColdPlanLine.crates),0)).join(ColdPlan,ColdPlan.id==ColdPlanLine.plan_id).where(ColdPlan.state=='confirmed',ColdPlanLine.consignment_id==consignment_id)
    if exclude_plan is not None:q=q.where(ColdPlan.id!=exclude_plan)
    return int(s.scalar(q)or 0)


def approved_sample(s,consignment_id):
    return s.scalar(select(VaseSample).where(VaseSample.consignment_id==consignment_id,VaseSample.state=='approved').order_by(VaseSample.id.desc()).limit(1))


def batch_snapshot(s,cons,outbound_at,plan_used,lots_used):
    sample=approved_sample(s,cons.id)
    if not sample:return None
    life=lifetime_pair(sample,cons,outbound_at)
    free=max(0,cons.crates-lots_used-plan_used)
    defendable=RANK[life['ambient_grade']]>RANK[sample.approved_grade] and RANK[life['cold_grade']]<=RANK[sample.approved_grade]
    return {'consignment_id':cons.id,'consignment_code':cons.code,'variety':cons.variety,'grower':cons.grower,'cut_at':cons.cut_at,'crates_total':cons.crates,'crates_locked_lots':lots_used,'crates_locked_plans':plan_used,'crates_free':free,'sample_id':sample.id,'sample_code':sample.code,'approved_grade':sample.approved_grade,'ambient_days':life['ambient_days'],'cold_days':life['cold_days'],'ambient_grade_est':life['ambient_grade'],'cold_grade_est':life['cold_grade'],'defendable':defendable,'eligible_crates':free if defendable else 0,'allocated_crates':0,'source_checksum':life['source_checksum'],'holdout_mae_days':life['holdout_mae_days'],'extrapolated':life['extrapolated']}


def snapshot_batches(s,outbound_at,exclude_plan=None):
    plan_used=plan_usage_by_consignment(s,exclude_plan)
    rows=[]
    for cons in s.scalars(select(Consignment).order_by(Consignment.id)):
        row=batch_snapshot(s,cons,outbound_at,plan_used.get(cons.id,0),lot_usage(s,cons.id))
        if row:rows.append(row)
    return rows


def allocate(batches,capacity):
    """冷位不足时优先保住等级更高、掉级更重的筐；同等级先分给常温寿命更短的批，再按批号。"""
    left=max(0,capacity)
    pool=sorted([b for b in batches if b['defendable']],key=lambda b:(RANK[b['approved_grade']],-(RANK[b['ambient_grade_est']]-RANK[b['approved_grade']]),b['ambient_days'],b['consignment_code']))
    for row in pool:
        take=min(row['eligible_crates'],left);row['allocated_crates']=take;left-=take
    return batches


def check_outbound(s,outbound_at):
    clk=s.get(Clock,1)
    if stamp(outbound_at)<=clk.now:raise HTTPException(409,'预计出库时刻须晚于业务时钟')
    return stamp(outbound_at)


def preview(s,outbound_at,cold_total):
    moment=check_outbound(s,outbound_at)
    if cold_total<0:raise HTTPException(422,'冷库总位不能为负数')
    locked=confirmed_usage(s);batches=snapshot_batches(s,moment);allocate(batches,max(0,cold_total-locked))
    return {'outbound_at':moment,'cold_total':cold_total,'locked_slots':locked,'free_slots':max(0,cold_total-locked),'preserved_crates':sum(b['allocated_crates'] for b in batches),'ambient_drop_crates':sum(b['eligible_crates'] for b in batches),'batches':batches}


def create(s,d,u):
    moment=check_outbound(s,d.outbound_at)
    live=require_room(s)
    total=live.total_slots if d.cold_total is None else d.cold_total
    locked=confirmed_usage(s);capacity=max(0,total-locked)
    batches={b['consignment_id']:b for b in snapshot_batches(s,moment)}
    if d.lines:
        picks=[]
        for line in d.lines:
            row=batches.get(line.consignment_id)
            if not row:raise HTTPException(409,'到场批次不存在或没有已主管核定的留样')
            if not row['defendable']:raise HTTPException(409,row['consignment_code']+' 常温不掉级或续冷无法保级，无需分位')
            if line.crates>row['crates_free']:raise HTTPException(409,row['consignment_code']+' 可分配筐数不足，请重新试算')
            picks.append((row,line.crates))
        if len({row['consignment_id'] for row,_ in picks})!=len(picks):raise HTTPException(409,'同批可拆筐，但只能分一条分位记录')
        if sum(c for _,c in picks)>capacity:raise HTTPException(409,'冷位余量不足，整单退回重新试算')
    else:
        allocate(list(batches.values()),capacity)
        picks=[(row,row['allocated_crates']) for row in batches.values() if row['allocated_crates']>0]
    if not picks:raise HTTPException(409,'没有可在续冷下保住核定等级的筐，无需建立冷位预案')
    code=(d.code or f'COLD-{int(time.time()*1000)}-{secrets.token_hex(2)}').strip()
    plan=ColdPlan(code=code,outbound_at=moment,cold_total=total,state='draft',note=d.note or '')
    s.add(plan);s.flush()
    for row,crates in picks:
        s.add(ColdPlanLine(plan_id=plan.id,consignment_id=row['consignment_id'],sample_id=row['sample_id'],crates=crates,ambient_days=row['ambient_days'],cold_days=row['cold_days'],ambient_grade=row['ambient_grade_est'],cold_grade=row['cold_grade_est'],approved_grade=row['approved_grade'],eligible_crates=row['eligible_crates']))
    s.flush();audit(s,u,'建立夜间冷位预案（不占位） '+plan.code)
    return view(s,plan.id)


def view(s,id):
    plan=s.get(ColdPlan,id)
    if not plan:raise HTTPException(404,'冷位预案不存在')
    lines=[pack(x) for x in s.scalars(select(ColdPlanLine).where(ColdPlanLine.plan_id==id).order_by(ColdPlanLine.id))]
    for x in lines:
        cons=s.get(Consignment,x['consignment_id']);x['consignment_code']=cons.code if cons else None
    return {**pack(plan),'lines':lines,'crates':sum(x['crates'] for x in lines),'preserved_crates':sum(x['crates'] for x in lines if RANK[x['cold_grade']]<=RANK[x['approved_grade']])}


def list_plans(s):
    return [view(s,x.id) for x in s.scalars(select(ColdPlan).order_by(ColdPlan.id.desc()))]


def confirm(s,id,u):
    if not s.get(ColdPlan,id):raise HTTPException(404,'冷位预案不存在')
    lines=list(s.scalars(select(ColdPlanLine).where(ColdPlanLine.plan_id==id).order_by(ColdPlanLine.consignment_id)))
    if not lines:raise HTTPException(409,'预案没有分位记录')
    # 锁序与挂场一致：先父行 Consignment（按ID升序），再冷库总位，最后预案本身
    for cid in sorted({x.consignment_id for x in lines}):lock(s,Consignment,cid)
    live=require_room(s,for_update=True)
    lock(s,ColdPlan,id);pre=s.get(ColdPlan,id,populate_existing=True)
    if pre.state=='outbound':raise HTTPException(409,'预案已出库，冷位与筐已放开')
    if pre.state=='confirmed':return view(s,id)
    clk=s.get(Clock,1)
    if pre.outbound_at<=clk.now:raise HTTPException(409,'预计出库时刻已过，请重新试算预案')
    locked=confirmed_usage(s,exclude_plan=id);crates=sum(x.crates for x in lines)
    if locked+crates>live.total_slots:raise HTTPException(409,'冷位余量不足，整单退回重新试算')
    plan_used=plan_usage_by_consignment(s,exclude_plan=id)
    for x in lines:
        cons=s.get(Consignment,x.consignment_id);used=lot_usage(s,cons.id)+plan_used.get(cons.id,0)
        if used+x.crates>cons.crates:raise HTTPException(409,cons.code+' 筐数余量不足，整单退回重新试算')
    pre.state='confirmed';pre.confirmed_by=u.email;pre.confirmed_at=clk.now;pre.cold_total=live.total_slots
    s.flush();audit(s,u,'主管确认冷位预案，锁定冷位与筐 '+pre.code)
    return view(s,id)


def outbound(s,id,u):
    if not s.get(ColdPlan,id):raise HTTPException(404,'冷位预案不存在')
    lock(s,ColdPlan,id)
    pre=s.get(ColdPlan,id,populate_existing=True)
    if pre.state=='outbound':return view(s,id)
    if pre.state!='confirmed':raise HTTPException(409,'只有主管已确认的预案可以记录出库')
    clk=s.get(Clock,1)
    pre.state='outbound';pre.outbound_recorded_at=clk.now
    s.flush();audit(s,u,'记录出库，腾出冷位并放开筐 '+pre.code)
    return view(s,id)


def room(s,for_update=False):
    q=select(ColdRoom).where(ColdRoom.id==1)
    return s.scalar(q.with_for_update() if for_update else q)


def require_room(s,for_update=False):
    x=room(s,for_update)
    if not x:raise HTTPException(409,'冷库总位尚未设置，请先由主管登记')
    return x


def capacity(s):
    x=room(s)
    if not x:return {'total_slots':0,'locked_slots':confirmed_usage(s),'free_slots':0,'configured':False}
    used=confirmed_usage(s)
    return {'total_slots':x.total_slots,'locked_slots':used,'free_slots':max(0,x.total_slots-used),'configured':True}


def set_capacity(s,d,u):
    x=room(s,for_update=True);used=confirmed_usage(s)
    if d.total_slots<used:raise HTTPException(409,'冷库总位不能低于已确认占用的 '+str(used)+' 位')
    if x:x.total_slots=d.total_slots
    else:s.add(ColdRoom(id=1,total_slots=d.total_slots))
    audit(s,u,'设置冷库总位 '+str(d.total_slots));s.flush()
    return capacity(s)
