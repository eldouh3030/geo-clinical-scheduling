"""Figure 6 of the paper (Section 5.7): queries allowed by the release rules, and response time.
Run after privacy-meta.py and the final-residency, final-noid and final-k10 experiments:
    python figure6.py
"""
import pandas as pd, numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
from scipy import stats
plt.rcParams.update({"font.family":"DejaVu Sans","font.size":9})
from pathlib import Path
ROOT=Path(__file__).resolve().parent
R=str(ROOT/"results")
ci=lambda x: 0 if len(x)<2 else stats.t.ppf(.975,len(x)-1)*np.std(x,ddof=1)/np.sqrt(len(x))
s=pd.read_csv(f"{R}/privacy-meta/summary.csv").set_index("rule")
fig,(a,b)=plt.subplots(1,2,figsize=(6.6,2.45),gridspec_kw={"width_ratios":[1,1.25]})
labels=["No identifiable\nresults","k = 5","k = 10","k = 20"]
strict=[s.loc["no-identifiable-results","allowed_share"]]+[s.loc[f"min-group-{k} (strict)","allowed_share"] for k in (5,10,20)]
len_=[s.loc["no-identifiable-results","allowed_share"]]+[s.loc[f"min-group-{k} (lenient)","allowed_share"] for k in (5,10,20)]
y=np.arange(len(labels))
a.barh(y+0.18,np.array(len_)*100,0.36,color="#76b7b2",label="lenient")
a.barh(y-0.18,np.array(strict)*100,0.36,color="#4e79a7",label="strict")
for yi,v in zip(y-0.18,strict): a.text(v*100+1.5,yi,f"{100*v:.1f}%",va="center",fontsize=7.5)
a.set_yticks(y); a.set_yticklabels(labels,fontsize=8); a.invert_yaxis()
a.set_xlim(0,100); a.set_xlabel("All-region queries allowed (%)"); a.grid(axis="x",alpha=0.3,lw=0.4)
a.legend(fontsize=7,loc="lower right"); a.set_title("(a) Queries that may be federated",fontsize=9)
exps=[("final-residency","Residency only","#bab0ac"),("final-noid","No identifiable results","#4e79a7"),("final-k10","k = 10","#f28e2b")]
for n,l,c in exps:
    d=pd.read_csv(f"{R}/{n}/results.csv"); d=d[d.policy=="flow"]
    g=d.groupby("load")["resp_mean_s"]; gr=d.groupby("load")["resp_mean_region_s"]
    b.errorbar(g.mean().index,g.mean().values*1000,yerr=g.apply(ci).values*1000,color=c,marker="o",ms=4,lw=1.3,capsize=2,label=l)
    b.plot(gr.mean().index,gr.mean().values*1000,color=c,ls="--",lw=1.0)
b.plot([],[],color="#555555",ls="--",lw=1.0,label="region-level queries only")
b.set_xticks([0.3,0.6,0.9,1.5,2.5]); b.set_xlabel("Load (x raw saturation)"); b.set_ylabel("Mean response time (ms)")
b.set_ylim(0,65); b.set_yticks([0,20,40,60]); b.grid(alpha=0.3,lw=0.4); b.legend(fontsize=6.5,loc="upper left",ncol=1); b.set_title("(b) Min-cost flow, completed queries",fontsize=9)
fig.tight_layout(); out=ROOT/"reports"/"privacy"; out.mkdir(parents=True,exist_ok=True); fig.savefig(out/"figure6-release-rules.png",dpi=300); print("written to", out/"figure6-release-rules.png")
