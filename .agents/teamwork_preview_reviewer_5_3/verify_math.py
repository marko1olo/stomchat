# Verify math models in Section 4.1.1 and Table 4.1.3

velocities = [5, 15, 30, 60, 120, 300]
T_base = 60
T_min = 45
T_max = 180
alpha = 0.40

print("=== CANONICAL NORMALIZED MODEL (epsilon=0) ===")
for V in velocities:
    v_eff = max(V, 5)
    f_vel = (30.0 / v_eff) ** alpha
    
    raw_day = T_base * f_vel * 0.85
    clamped_day = max(T_min, min(int(raw_day), T_max))
    
    raw_eve = T_base * f_vel * 1.00
    clamped_eve = max(T_min, min(int(raw_eve), T_max))
    
    raw_night = T_base * f_vel * 1.60
    clamped_night = max(T_min, min(int(raw_night), T_max))
    
    print(f"V={V:3d}: Day={clamped_day:3d} ({raw_day:.1f}m), Eve={clamped_eve:3d} ({raw_eve:.1f}m), Night={clamped_night:3d} ({raw_night:.1f}m)")

print("\n=== SMOOTHED MODEL (epsilon=1.0) ===")
for V in velocities:
    v_eff = max(V, 5)
    f_vel = (30.0 / (v_eff + 1.0)) ** alpha
    
    raw_day = T_base * f_vel * 0.85
    clamped_day = max(T_min, min(int(raw_day), T_max))
    
    raw_eve = T_base * f_vel * 1.00
    clamped_eve = max(T_min, min(int(raw_eve), T_max))
    
    raw_night = T_base * f_vel * 1.60
    clamped_night = max(T_min, min(int(raw_night), T_max))
    
    print(f"V={V:3d}: Day={clamped_day:3d} ({raw_day:.1f}m), Eve={clamped_eve:3d} ({raw_eve:.1f}m), Night={clamped_night:3d} ({raw_night:.1f}m)")
