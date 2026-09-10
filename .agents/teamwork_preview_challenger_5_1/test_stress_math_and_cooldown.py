#!/usr/bin/env python3
"""
Empirical stress-testing of mathematical models, formulas, and edge cases
from REPORT_CHAT_BALANCE_AND_LOGS.md.
"""

import math
from datetime import datetime, timedelta

def formula_from_section_4_1_1(V: float, H: int) -> tuple[float, float]:
    """
    Mathematical formulation from Section 4.1.1:
    T_cooldown(V, H) = clamp( T_base * (V_target / (V_eff + epsilon))^alpha * K_diurnal(H), T_min, T_max )
    T_base = 60
    V_target = 30
    V_eff = max(V, 5)
    epsilon = 1.0
    alpha = 0.40
    T_min = 45
    T_max = 180
    """
    T_base = 60.0
    V_target = 30.0
    V_eff = max(V, 5.0)
    epsilon = 1.0
    alpha = 0.40
    T_min = 45.0
    T_max = 180.0

    if 9 <= H <= 19:
        K = 0.85
    elif 19 < H <= 23:
        K = 1.00
    else:
        K = 1.60

    ratio = V_target / (V_eff + epsilon)
    raw = T_base * (ratio ** alpha) * K
    clamped = max(T_min, min(raw, T_max))
    return raw, clamped

def code_diff2_calculation(V: float, H: int) -> tuple[float, int]:
    """
    Code implementation from Diff 2:
    v_eff = max(velocity, 5)
    f_vel = (30.0 / (v_eff + 1.0)) ** 0.40
    if 9 <= msk_hour <= 19:
        f_time = 0.85
    elif 19 < msk_hour <= 23:
        f_time = 1.00
    else:
        f_time = 1.60
    raw_cd = config.PASSIVE_COOLDOWN_BASE_MINUTES * f_vel * f_time
    cd_minutes = int(max(config.PASSIVE_COOLDOWN_MIN_MINUTES,
                         min(raw_cd, config.PASSIVE_COOLDOWN_MAX_MINUTES)))
    """
    PASSIVE_COOLDOWN_BASE_MINUTES = 60
    PASSIVE_COOLDOWN_MIN_MINUTES = 45
    PASSIVE_COOLDOWN_MAX_MINUTES = 180

    v_eff = max(V, 5)
    f_vel = (30.0 / (v_eff + 1.0)) ** 0.40

    if 9 <= H <= 19:
        f_time = 0.85
    elif 19 < H <= 23:
        f_time = 1.00
    else:
        f_time = 1.60

    raw_cd = PASSIVE_COOLDOWN_BASE_MINUTES * f_vel * f_time
    cd_minutes = int(max(PASSIVE_COOLDOWN_MIN_MINUTES,
                         min(raw_cd, PASSIVE_COOLDOWN_MAX_MINUTES)))
    return raw_cd, cd_minutes

def test_table_4_1_3_reproducibility():
    """
    Compare Table 4.1.3 values in REPORT_CHAT_BALANCE_AND_LOGS.md with actual formula output.
    Table 4.1.3 claims:
    V=5:   Day: 108 min, Eve: 127 min, Night: 180 min (max)
    V=15:  Day: 67 min,  Eve: 79 min,  Night: 126 min
    V=30:  Day: 51 min,  Eve: 60 min,  Night: 96 min
    V=60:  Day: 45 min,  Eve: 46 min,  Night: 74 min
    V=120: Day: 45 min,  Eve: 45 min,  Night: 56 min
    V=300: Day: 45 min,  Eve: 45 min,  Night: 45 min
    """
    print("=== TEST 1: REPRODUCIBILITY OF TABLE 4.1.3 ===")
    table_claims = {
        5:   {"day": 108, "eve": 127, "night": 180},
        15:  {"day": 67,  "eve": 79,  "night": 126},
        30:  {"day": 51,  "eve": 60,  "night": 96},
        60:  {"day": 45,  "eve": 46,  "night": 74},
        120: {"day": 45,  "eve": 45,  "night": 56},
        300: {"day": 45,  "eve": 45,  "night": 45},
    }

    discrepancies = []
    for V, claims in table_claims.items():
        _, day_cd = code_diff2_calculation(V, 12)    # MSK 12:00 -> day
        _, eve_cd = code_diff2_calculation(V, 20)    # MSK 20:00 -> eve
        _, night_cd = code_diff2_calculation(V, 2)   # MSK 02:00 -> night

        day_diff = day_cd - claims["day"]
        eve_diff = eve_cd - claims["eve"]
        night_diff = night_cd - claims["night"]

        print(f"V={V:3d} | Day: claimed={claims['day']:3d}, got={day_cd:3d} (diff={day_diff:+2d}) | "
              f"Eve: claimed={claims['eve']:3d}, got={eve_cd:3d} (diff={eve_diff:+2d}) | "
              f"Night: claimed={claims['night']:3d}, got={night_cd:3d} (diff={night_diff:+2d})")

        if day_diff != 0 or eve_diff != 0 or night_diff != 0:
            discrepancies.append((V, day_diff, eve_diff, night_diff))

    print(f"Total velocity rows with arithmetic discrepancies: {len(discrepancies)} of {len(table_claims)}")

def test_extreme_velocity_boundaries():
    print("\n=== TEST 2: EXTREME VELOCITY BOUNDARIES ===")
    test_velocities = [
        -1000, -10, -1, 0, 0.0001, 1, 4, 5, 10, 30, 100, 500, 1000, 10000, 1000000, float('inf')
    ]
    for V in test_velocities:
        try:
            if math.isinf(V):
                # inf test
                v_eff = max(V, 5)
                f_vel = (30.0 / (v_eff + 1.0)) ** 0.40
            raw, cd = code_diff2_calculation(V, 14)
            print(f"V = {V:10} -> raw = {raw:8.2f} min, clamped = {cd:3d} min")
            assert 45 <= cd <= 180, f"Clamping violation: {cd} not in [45, 180]"
        except Exception as e:
            print(f"V = {V:10} -> CRASH: {e}")

def test_diurnal_hourly_continuity():
    print("\n=== TEST 3: DIURNAL HOURLY CONTINUITY & BOUNDARIES ===")
    for h in range(24):
        raw_day, cd = code_diff2_calculation(30, h)
        if 9 <= h <= 19:
            regime = "Daytime (0.85)"
        elif 19 < h <= 23:
            regime = "Evening (1.00)"
        else:
            regime = "Night   (1.60)"
        print(f"Hour {h:02d}:00 MSK -> {regime} -> cd = {cd:3d} min (raw={raw_day:.1f})")

def test_dual_volume_bypass_scenarios():
    print("\n=== TEST 4: DUAL-CONDITION VOLUME BYPASS GATE SCENARIOS ===")
    # Dual condition: delta_t >= 45 min AND M_since >= 40
    scenarios = [
        # (delta_t_min, M_since, expected_bypass)
        (0, 0, False),
        (30, 100, False),   # M_since high but delta_t < 45m -> NOT bypassed
        (44, 500, False),   # delta_t 44m < 45m -> NOT bypassed
        (45, 39, False),    # delta_t 45m, but M_since 39 < 40 -> NOT bypassed
        (45, 40, True),     # delta_t 45m, M_since 40 -> BYPASSED
        (50, 15, False),    # delta_t 50m, M_since 15 (if normal cd is 60m)
        (60, 10, True),     # delta_t 60m >= normal cd (60m) -> Cleared by time
        (46, 1000, True),   # High volume burst after 46m -> BYPASSED
    ]
    for dt, m_since, expected in scenarios:
        # Assume normal cooldown is 60 min
        normal_cd = 60
        cleared_by_time = dt >= normal_cd
        cleared_by_bypass = (dt >= 45 and m_since >= 40)
        overall_cleared = cleared_by_time or cleared_by_bypass
        print(f"dt={dt:2d}m, M_since={m_since:4d} | time_clear={cleared_by_time} | bypass={cleared_by_bypass} | overall={overall_cleared}")

if __name__ == "__main__":
    test_table_4_1_3_reproducibility()
    test_extreme_velocity_boundaries()
    test_diurnal_hourly_continuity()
    test_dual_volume_bypass_scenarios()
