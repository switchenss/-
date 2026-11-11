# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""用统一规则参数生成三份通达信日线 L2 公式。"""
import argparse
import json
from pathlib import Path

from engine import LABELS, history

HERE = Path(__file__).resolve().parent
KEYS = "FAST SLOW SLOPE VN FN DN CN AN NP JUMP VREL ACAP ASH ATH APMIN VMODE LVR".split()
COLORS = ["COLORBLUE", "COLORYELLOW", "COLORRED", "COLOR008CFF", "COLORGREEN"]
CORE = """{仅用于具备逐笔资金流向函数权限的沪深股票日线}
{所有信号仅供技术复盘参考 不构成任何交易建议}
LB:=L2_AMO(0,0)+L2_AMO(1,0);
LS:=L2_AMO(0,1)+L2_AMO(1,1);
AB:=ACTINVOL;
AV:=ACTOUTVOL;
L2OK:=PERIOD=5 AND VOL>0 AND LB>=0 AND LS>=0 AND AB>=0 AND AV>=0
      AND LB+LS>0 AND AB+AV>0;
IMB:=100*(LB-LS)/MAX(LB+LS,1);
ACTIVEP:=100*(AB-AV)/MAX(AB+AV,1);
FM:=MA(IMB,PFN);
FMA:=MA(C,PFAST);
SMA2:=MA(C,PSLOW);
FALLING:=C<SMA2 AND FMA<SMA2 AND FMA<REF(FMA,PSLOPE);
RBASE:=REF(MA(VOL,PVN),1);
RV:=VOL/MAX(RBASE,0.0001);
SUPP:=REF(LLV(L,PDN),1);
RESI:=REF(HHV(H,PDN),1);
TRX:=MAX(H-L,MAX(ABS(H-REF(C,1)),ABS(L-REF(C,1))));
ATRP:=100*MA(TRX,PAN)/MAX(C,0.0001);
ASHOCK:=ATRP>=PATH AND ATRP>=PASH*REF(MA(ATRP,PVN),1);
GOOD:=BARSCOUNT(C)>=PNEED AND COUNT(VOL>0,PNEED)=PNEED
      AND COUNT(L2OK,PFN+PDN)=PFN+PDN AND RBASE>0;
POSDIV:=C<=REF(C,PDN) AND FM>REF(FM,PDN);
NEGDIV:=C>=REF(C,PDN) AND FM<REF(FM,PDN);
{观察 预警 买入观察 风险预警 卖出观察原始状态}
W0:=GOOD AND L<=SUPP*(1+PNP/100) AND FM>REF(FM,1);
A0:=W0 AND FM-REF(FM,1)>=PJUMP AND (RV>=PVREL OR POSDIV);
R0:=GOOD AND (ASHOCK OR (C<FMA AND FM<0 AND (RV>=PVREL OR NEGDIV OR FALLING)));
S0:=GOOD AND COUNT(R0,PCN+1)>0 AND C<SUPP AND C<SMA2 AND FM<0 AND ACTIVEP<0;
B0:=GOOD AND COUNT(A0,PCN+1)>0 AND R0=0 AND S0=0 AND C>REF(H,1)
    AND FM>0 AND ATRP<=PACAP AND C>FMA AND ACTIVEP>=PAPMIN;
{最新一根可加即时过滤 仅约束偏多三种显示和预警}
LIVEOK:=PVMODE=0 OR ISLASTBAR=0 OR (DYNAINFO(17)>=PLVR AND
        DYNAINFO(22)+DYNAINFO(23)>0 AND DYNAINFO(23)>=DYNAINFO(22));
W1:=W0 AND REF(W0,1)=0 AND LIVEOK;
A1:=A0 AND REF(A0,1)=0 AND LIVEOK;
B1:=B0 AND REF(B0,1)=0 AND LIVEOK;
R1:=R0 AND REF(R0,1)=0;
S1:=S0 AND REF(S0,1)=0;
{从高到低优先级输出 每根K线最多一个事件}
F5:=S1;
F4:=R1 AND F5=0;
F3:=B1 AND F5=0 AND F4=0;
F2:=A1 AND F5=0 AND F4=0 AND F3=0;
F1:=W1 AND F5=0 AND F4=0 AND F3=0 AND F2=0;
"""
PANEL = """零轴:IF(L2OK,0,DRAWNULL),COLORGRAY;
STICKLINE(L2OK AND IMB>=0,0,IMB,2,0),COLORRED;
STICKLINE(L2OK AND IMB<0,0,IMB,2,0),COLORGREEN;
大单失衡:IF(L2OK,IMB,DRAWNULL),NODRAW;
资金平滑:IF(COUNT(L2OK,PFN)=PFN,FM,DRAWNULL),COLORWHITE;
主动失衡:IF(L2OK,ACTIVEP,DRAWNULL),COLORCYAN;
日量倍数:IF(PERIOD=5 AND RBASE>0,RV,DRAWNULL),NODRAW;
支撑参考:IF(PERIOD=5,SUPP,DRAWNULL),NODRAW;
阻力参考:IF(PERIOD=5,RESI,DRAWNULL),NODRAW;
波动百分比:IF(PERIOD=5,ATRP,DRAWNULL),NODRAW;
L2有效:L2OK,NODRAW;
历史齐全:GOOD,NODRAW;
"""
NOTICE = "{PASIG 0任一 1观察 2预警 3买入观察 4风险预警 5卖出观察}\n"


def generate(p, root):
    values = {key: p[key] for key in KEYS}
    values["NEED"] = history(p)
    if any(type(x) not in (int, float) for x in values.values()):
        raise ValueError("公式参数须为数值")
    header = "".join(f"P{key}:={value};\n" for key, value in values.items())
    labels = "".join(f"{name}:F{i},NODRAW;\n" for i, name in enumerate(LABELS, 1))
    marks = "".join(
        f"DRAWTEXT(F{i},{'L*0.99' if i<=3 else 'H*1.01'},'{name}'),{color};\n"
        for i, (name, color) in enumerate(zip(LABELS, COLORS), 1)
    )
    expression = " OR ".join(f"(PASIG={i} AND F{i})" for i in range(1, 6))
    alert = f"XG:(PASIG=0 AND (F1 OR F2 OR F3 OR F4 OR F5)) OR {expression};\n"
    root.mkdir(parents=True, exist_ok=True)
    for filename, title, setup, tail in [
        ("01_CN_MAIN.txt", "A股主图", "", labels + marks),
        ("02_CN_FLOW.txt", "A股副图", "", PANEL + labels),
        ("03_CN_ALERT.txt", "A股预警", NOTICE + f"PASIG:={p['ASIG']};\n", alert),
    ]:
        (root / filename).write_text(f"{{【{title}】}}\n{header}{setup}{CORE}{tail}",
                                     encoding="utf-8-sig")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=HERE / "config.json")
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args()
    from gs import validate

    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    validate(config)
    generate(config["signals"], args.out)
