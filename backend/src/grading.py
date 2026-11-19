import hashlib
from datetime import datetime
from functools import lru_cache
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error


@lru_cache(maxsize=1)
def _trained():
    rng=np.random.default_rng(35);x=np.column_stack([rng.uniform(.1,1,200),rng.uniform(35,90,200),rng.uniform(1,40,200),rng.uniform(0,25,200)])
    y=np.clip(14-6*x[:,0]+.04*(x[:,1]-50)-.13*x[:,2]+.09*x[:,3]+rng.normal(0,.35,200),2,20)
    tr,te=train_test_split(np.arange(200),test_size=.25,random_state=35)
    model=GradientBoostingRegressor(n_estimators=80,max_depth=2,random_state=35);model.fit(x[tr],y[tr])
    return model,float(mean_absolute_error(y[te],model.predict(x[te]))),tr.tolist(),te.tolist(),hashlib.sha256(np.column_stack([x,y]).tobytes()).hexdigest()


def _days(model,maturity,stem_cm,postcut_hours,cold_hours):
    return float(model.predict([[maturity,stem_cm,postcut_hours,cold_hours]])[0])


def _postcut(cons):
    return (datetime.fromisoformat(cons.arrived_at)-datetime.fromisoformat(cons.cut_at)).total_seconds()/3600


def predict(sample,cons):
    model,mae,tr,te,checksum=_trained();hours=_postcut(cons)
    features=[sample.maturity,sample.stem_cm,hours,sample.cold_hours];estimate=_days(model,*features)
    return {'model':'GradientBoostingRegressor','seed':35,'train_rows':150,'holdout_rows':50,'train_indices':tr,'holdout_indices':te,'source_checksum':checksum,'input_snapshot':dict(zip(['maturity','stem_cm','postcut_hours','cold_hours'],features)),'vase_days':round(estimate,3),'suggested_grade':grade_for(estimate),'holdout_mae_days':round(mae,4),'threshold_days':{'A':10,'B':7,'C':0}}


def lifetime_pair(sample,cons,outbound_at):
    """Estimate vase life at an expected outbound time,有两种口径：
    常温=不再续冷，冷藏小时停留在留样登记值；
    续冷=从到场到出库一直留在冷库，冷藏小时增加在场时长。
    两种口径都用同一采后小时（采切到出库），便于同口径比较。"""
    model,mae,_,_,checksum=_trained()
    outbound=outbound_at if isinstance(outbound_at,datetime) else datetime.fromisoformat(outbound_at)
    postcut=(outbound-datetime.fromisoformat(cons.cut_at)).total_seconds()/3600
    on_site=(outbound-datetime.fromisoformat(cons.arrived_at)).total_seconds()/3600
    continued=round(sample.cold_hours+max(0.0,on_site),3)
    ambient=_days(model,sample.maturity,sample.stem_cm,postcut,sample.cold_hours)
    cold=_days(model,sample.maturity,sample.stem_cm,postcut,continued)
    return {'model':'GradientBoostingRegressor','seed':35,'source_checksum':checksum,'holdout_mae_days':round(mae,4),'postcut_hours':round(postcut,3),'continued_cold_hours':continued,'ambient_days':round(ambient,3),'cold_days':round(cold,3),'ambient_grade':grade_for(ambient),'cold_grade':grade_for(cold),'training_ranges':{'maturity':[.1,1],'stem_cm':[35,90],'postcut_hours':[1,40],'cold_hours':[0,25]},'extrapolated':bool(postcut>40 or continued>25 or sample.cold_hours>25)}


def grade_for(days):
    return 'A' if days>=10 else 'B' if days>=7 else 'C'
