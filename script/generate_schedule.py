#!/usr/bin/env python3
"""Create a counterbalanced PDF schedule from the appointment export."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle


ROOT_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = ROOT_DIR / "input"
OUTPUT_FILE = ROOT_DIR / "output" / "terminuebersicht.pdf"
REQUIRED_COLUMNS = {"Startdatum", "Von", "Name", "E-Mail", "Status"}


@dataclass(frozen=True)
class Appointment:
    date: date
    time: str
    name: str
    email: str


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("muss eine positive ganze Zahl sein") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("muss mindestens 1 sein")
    return number


def filename(value: str) -> str:
    if not value or Path(value).name != value or value in {".", ".."}:
        raise argparse.ArgumentTypeError("bitte nur einen Dateinamen ohne Pfad angeben")
    if Path(value).suffix.casefold() != ".csv":
        raise argparse.ArgumentTypeError("die Eingabedatei muss eine CSV-Datei sein")
    return value


def find_csv(input_dir: Path, input_filename: str | None = None) -> Path:
    if input_filename is not None:
        csv_path = input_dir / input_filename
        if not csv_path.is_file():
            raise ValueError(f"CSV-Datei nicht gefunden: {csv_path}")
        return csv_path

    csv_files = sorted(input_dir.glob("*.csv"))
    if len(csv_files) != 1:
        raise ValueError(
            f"Im Ordner {input_dir} wurde "
            f"{'keine' if not csv_files else 'mehr als eine'} CSV-Datei gefunden."
        )
    return csv_files[0]


def load_confirmed_appointments(csv_path: Path) -> list[Appointment]:
    appointments: list[Appointment] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file, delimiter=";")
        columns = set(reader.fieldnames or [])
        missing_columns = REQUIRED_COLUMNS - columns
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise ValueError(f"CSV-Spalten fehlen: {missing}")

        for line_number, row in enumerate(reader, start=2):
            if (row.get("Status") or "").strip().casefold() != "bestätigt":
                continue

            date_value = (row.get("Startdatum") or "").strip()
            time_value = (row.get("Von") or "").strip()
            name = (row.get("Name") or "").strip()
            email = (row.get("E-Mail") or "").strip()
            if not all((date_value, time_value, name, email)):
                raise ValueError(
                    f"Bestätigter Termin in CSV-Zeile {line_number} "
                    "hat ein leeres Datum, eine leere Uhrzeit, einen leeren Namen "
                    "oder eine leere E-Mail-Adresse."
                )

            try:
                appointment_date = datetime.strptime(date_value, "%d.%m.%Y").date()
                parsed_time = datetime.strptime(time_value, "%H:%M").strftime("%H:%M")
            except ValueError as exc:
                raise ValueError(
                    f"Ungültiges Datum oder ungültige Uhrzeit in CSV-Zeile {line_number}."
                ) from exc

            appointments.append(
                Appointment(
                    date=appointment_date,
                    time=parsed_time,
                    name=name,
                    email=email,
                )
            )

    if not appointments:
        raise ValueError("Die CSV-Datei enthält keine bestätigten Termine.")
    return appointments


def latin_square_scenario(participant_index: int, visit_index: int) -> int:
    """Return a scenario from the repeated 2x2 cyclic Latin square."""
    return 1 + (participant_index + visit_index) % 2


def create_pdf(appointments: list[Appointment], target_visits: int, output_file: Path) -> None:
    participants: dict[str, list[Appointment]] = defaultdict(list)
    for appointment in appointments:
        participants[appointment.email.casefold()].append(appointment)

    ordered_participants = sorted(
        participants.items(),
        key=lambda item: (
            min((appointment.date, appointment.time) for appointment in item[1]),
            item[0],
        ),
    )

    appointments_by_date: dict[date, list[tuple[Appointment, int]]] = defaultdict(list)
    participant_counts: list[int] = []
    for participant_index, (email_key, participant_appointments) in enumerate(
        ordered_participants
    ):
        ordered_appointments = sorted(
            participant_appointments, key=lambda appointment: (appointment.date, appointment.time)
        )
        participant_counts.append(len(ordered_appointments))
        for visit_index, appointment in enumerate(ordered_appointments):
            appointments_by_date[appointment.date].append(
                (appointment, latin_square_scenario(participant_index, visit_index))
            )

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "ScheduleCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        wordWrap="CJK",
    )
    centered_cell_style = ParagraphStyle(
        "CenteredScheduleCell",
        parent=cell_style,
        alignment=TA_CENTER,
    )

    story = [
        Paragraph("Übersicht der Messtermine", styles["Title"]),
        Paragraph(
            f"Bestätigte Termine: {len(appointments)}"
            f" &nbsp;&nbsp; Teilnehmende: {len(ordered_participants)}"
            f" &nbsp;&nbsp; Ziel je Person: {target_visits}",
            styles["BodyText"],
        ),
    ]
    deviations = sum(count != target_visits for count in participant_counts)
    if deviations:
        story.append(
            Paragraph(
                f"Hinweis: Bei {deviations} von {len(ordered_participants)} "
                f"Teilnehmenden weicht die Zahl bestätigter Termine vom Ziel "
                f"{target_visits} ab.",
                styles["BodyText"],
            )
        )

    column_widths = [75, 58, 160, 340, 75]
    for appointment_date in sorted(appointments_by_date):
        story.append(Paragraph(appointment_date.strftime("%d.%m.%Y"), styles["Heading2"]))
        rows = [["Datum", "Uhrzeit", "Name", "E-mail", "Szenario"]]
        day_appointments = sorted(
            appointments_by_date[appointment_date],
            key=lambda item: (item[0].time, item[0].name.casefold()),
        )
        for appointment, scenario in day_appointments:
            rows.append(
                [
                    Paragraph(appointment.date.strftime("%d.%m.%Y"), cell_style),
                    Paragraph(escape(appointment.time), centered_cell_style),
                    Paragraph(escape(appointment.name), cell_style),
                    Paragraph(escape(appointment.email), cell_style),
                    Paragraph(str(scenario), centered_cell_style),
                ]
            )

        table = Table(rows, colWidths=column_widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24476b")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 9),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#b8c2cc")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef3f8")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(table)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output_file),
        pagesize=landscape(A4),
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title="Übersicht der Messtermine",
    )
    document.build(story)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Erstellt einen nach Szenario gegenbalancierten PDF-Messplan."
    )
    parser.add_argument(
        "-n",
        type=positive_integer,
        required=True,
        metavar="ANZAHL",
        help="erwartete Zahl bestätigter Termine pro teilnehmender Person",
    )
    
    parser.add_argument(
        "-fn",
        "--filename",
        type=filename,
        required=False,
        metavar="FILENAME",
        help="Dateiname der CSV-Datei im input-Ordner (ohne Pfadangabe)",
    )

    args = parser.parse_args()

    try:
        csv_path = find_csv(INPUT_DIR, args.filename)
        appointments = load_confirmed_appointments(csv_path)
        create_pdf(appointments, args.n, OUTPUT_FILE)
    except (OSError, UnicodeDecodeError, csv.Error, ValueError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1

    print(f"PDF erstellt: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
