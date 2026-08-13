import pickle, numpy as np

p = r"C:\Users\Josep\Downloads\step3_build18\step3\artifacts\calibrator.pkl"
d = pickle.load(open(p, "rb"))
x, y = np.asarray(d["x"]), np.asarray(d["y"])
n, k = np.asarray(d["n"], float), np.asarray(d["k"], float)

print("knots:", len(x), " obs:", n.sum(), " defaults:", k.sum())
print("calib base rate:", k.sum() / n.sum())

m = y <= y.min() + 1e-12
print("bottom block:", int(m.sum()), "knots,",
      int(n[m].sum()), "observations,", int(k[m].sum()), "defaults")