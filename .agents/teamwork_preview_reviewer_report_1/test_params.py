import math

# Let's test different formulas
def test_calc(t_base, v_target, alpha, eps):
    print(f"--- t_base={t_base}, v_target={v_target}, alpha={alpha}, eps={eps} ---")
    for v in [5, 15, 30, 60, 120, 300]:
        f_vel = (v_target / (v + eps)) ** alpha
        day = max(45, min(180, round(t_base * f_vel * 0.85)))
        eve = max(45, min(180, round(t_base * f_vel * 1.00)))
        ngt = max(45, min(180, round(t_base * f_vel * 1.60)))
        print(f"V={v:3d}: Day={day}m, Eve={eve}m, Ngt={ngt}m")

test_calc(60.0, 30.0, 0.42, 0.0)
test_calc(60.0, 30.0, 0.43, 0.0)
test_calc(60.0, 30.0, 0.45, 0.0)
test_calc(60.0, 32.0, 0.40, 0.0)
test_calc(60.0, 30.0, 0.41, 0.0)
test_calc(60.0, 30.0, 0.40, -0.5)
