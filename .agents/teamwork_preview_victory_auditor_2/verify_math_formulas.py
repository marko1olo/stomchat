import math

print("=== INDEPENDENT MATHEMATICAL VERIFICATION OF TABLE 4.1.3 ===")

T_base = 60.0
alpha = 0.40
T_min = 45.0
T_max = 180.0

diurnal = {
    "Day (0.85)": 0.85,
    "Evening (1.00)": 1.00,
    "Night (1.60)": 1.60
}

velocities = [5, 15, 30, 60, 120, 300]

print(f"{'Velocity V':<12} | {'Day (0.85)':<15} | {'Evening (1.00)':<15} | {'Night (1.60)':<15}")
print("-" * 65)

for v in velocities:
    row_str = f"{v:<12} | "
    v_eff = max(v, 5.0)
    f_vel = (30.0 / v_eff) ** alpha
    
    for period, k in diurnal.items():
        raw_t = T_base * f_vel * k
        clamped_t = max(T_min, min(T_max, raw_t))
        int_t = round(clamped_t)
        row_str += f"{int_t}m ({raw_t:.1f}m)      | "
    print(row_str)
