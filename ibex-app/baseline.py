import pickle, numpy as np
from scipy.stats import beta as Beta

P = r"C:\Users\Josep\Downloads\step3_build18\step3\artifacts\calibrator.pkl"
d = pickle.load(open(P, "rb"))
n = np.asarray(d["n"], float); k = np.asarray(d["k"], float)
N, K = n.sum(), k.sum(); pbar = K / N
f = 40.0/np.log(2); off = 600.0 - f*np.log(20.0)
sc = lambda p: off + f*np.log((1-p)/p)

cum_n, cum_k = np.cumsum(n), np.cumsum(k)
i = int(np.argmax(cum_k > 0)); N0 = float(cum_n[i-1]); K0 = 0.0
print("base rate %.5f   bottom block n=%.0f k=0\n" % (pbar, N0))

for m in (20, 50, 100, 200, 500):
    a, b = pbar*m, (1-pbar)*m
    pm = (K0+a)/(N0+m)
    hi = Beta.ppf(0.95, K0+a, N0-K0+b)
    print("prior m=%4d  post mean %.5f -> %6.1f    95%% upper %.5f -> %6.1f"
          % (m, pm, sc(pm), hi, sc(hi)))

print("\nrule of three  %.5f -> %6.1f" % (3.0/N0, sc(3.0/N0)))
print("current floor  %.5f -> %6.1f" % (d["pd_floor"], sc(d["pd_floor"])))
print("obs needed to justify current floor: %.0f" % (3.0/d["pd_floor"]))