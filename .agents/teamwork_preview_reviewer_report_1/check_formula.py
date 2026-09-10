import math

def calc_t(v, k, alpha=0.4, v_target=30.0, eps=1.0, t_base=60.0):
    v_eff = max(v, 5)
    f_vel = (v_target / (v_eff + eps)) ** alpha
    raw = t_base * f_vel * k
    return max(45, min(180, int(round(raw))))

velocities = [5, 15, 30, 60, 120, 300]
ks = [0.85, 1.00, 1.60]

print("With eps=1.0, v_eff=max(v,5):")
for v in velocities:
    row = [calc_t(v, k) for k in ks]
    print(f"V={v:3d}: Day={row[0]}m, Eve={row[1]}m, Night={row[2]}m")

print("\nWhat if eps=0.0 and v_eff=max(v, 1) or v_eff=v?")
for v in velocities:
    f_vel = (30.0 / v) ** 0.4
    row = [max(45, min(180, int(round(60.0 * f_vel * k)))) for k in ks]
    print(f"V={v:3d}: Day={row[0]}m, Eve={row[1]}m, Night={row[2]}m")
