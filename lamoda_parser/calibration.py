"""Этап 3: сравнение оценки продаж с реальными продажами JOTO из API Lamoda.

Критерий успеха MVP: суммарная ошибка не больше 20–25%.

    python -m lamoda_parser.calibration estimated.csv actual.csv

Оба CSV: колонки sku,units (построчно по товару за один и тот же период).
Реальные продажи выгружаются из lamoda-bot (API Lamoda для селлеров).
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

TARGET_ERROR = 0.25


@dataclass
class CalibrationResult:
    total_estimated: int
    total_actual: int
    total_error: float        # |оценка − факт| / факт по всей выборке
    wape: float               # Σ|оценка − факт| / Σ факт по товарам
    skus_compared: int
    missing_in_estimate: list[str]
    passed: bool


def compare(estimated: dict[str, int], actual: dict[str, int], target: float = TARGET_ERROR) -> CalibrationResult:
    skus = sorted(actual)
    if not skus:
        raise ValueError("нет реальных продаж для сравнения")
    total_act = sum(actual[s] for s in skus)
    total_est = sum(estimated.get(s, 0) for s in skus)
    if total_act == 0:
        raise ValueError("реальные продажи за период равны нулю")
    abs_err = sum(abs(estimated.get(s, 0) - actual[s]) for s in skus)
    total_error = abs(total_est - total_act) / total_act
    return CalibrationResult(
        total_estimated=total_est,
        total_actual=total_act,
        total_error=round(total_error, 4),
        wape=round(abs_err / total_act, 4),
        skus_compared=len(skus),
        missing_in_estimate=[s for s in skus if s not in estimated],
        passed=total_error <= target,
    )


def read_units(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sku = row["sku"].strip()
            out[sku] = out.get(sku, 0) + int(float(row["units"]))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__)
        return 2
    r = compare(read_units(Path(argv[0])), read_units(Path(argv[1])))
    print(f"Товаров: {r.skus_compared}")
    print(f"Оценка: {r.total_estimated} шт, факт: {r.total_actual} шт")
    print(f"Ошибка по сумме: {r.total_error:.1%}  (цель ≤ {TARGET_ERROR:.0%})")
    print(f"WAPE по товарам: {r.wape:.1%}")
    if r.missing_in_estimate:
        print(f"Нет во внешних данных: {len(r.missing_in_estimate)} артикулов")
    print("ИДЁМ ДАЛЬШЕ" if r.passed else "НЕТОЧНО — ищем другой способ оценки")
    return 0 if r.passed else 1


if __name__ == "__main__":
    sys.exit(main())
