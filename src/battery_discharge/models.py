"""Shared record contract; all internal capacity/energy units are Ah/Wh."""
POINT_FIELDS = {
    'elapsed_s': '时间 (s)', 'voltage_v': '电压 (V)', 'current_a': '电流 (A)',
    'temperature_c': '仪器温度 (℃)', 'power_w': '功率 (W)',
    'capacity_ah': '累计放电容量 (Ah)', 'energy_wh': '累计放电能量 (Wh)',
    'remaining_pct': '本次相对剩余容量 (%)', 'soc_pct': '相对 SOC (%)',
}
FORMULA_VERSION = 'trapezoid-v1'
# Candidate is a dict with metadata, points, issues (list[str]), source_file,
# source_sheet, source_hash, fingerprint, summary, approved (default False).
# metadata keys: form_factor, category_code, sample_code, model_name,
# manufacturer, supplier, condition, nominal_capacity_ah, test_date,
# recorded_start, set_current_a, cutoff_voltage_v, mode, mapping_source,
# mapping_row, initial_soc_pct, reference_capacity_ah, soc_basis.
# points: source_row, raw_time, elapsed_s, voltage_v, current_a,
# temperature_c, power_w, capacity_ah, energy_wh, remaining_pct, soc_pct.
# summary: total_capacity_ah, total_energy_wh, duration_s,
# mean_voltage_v, integration_valid, formula_version.
