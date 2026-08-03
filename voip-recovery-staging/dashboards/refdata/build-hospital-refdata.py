#!/usr/bin/env python3
"""Generate dashboards/refdata/hospital.json — doctor / branch / specialty catalogue.

Covers ALL 25 Sri Lankan districts (branches) and 20 specialties, with TWO
doctors available for every (district x specialty) pair so the agent can offer a
real choice. Doctor names + time slots are intentionally REUSED across entries —
this is mock/demo data, that is fine and expected.

The lab `tests` catalogue is preserved verbatim from the existing file. Lab is
now a SEPARATE service (order_lab_test, a dormant flow activated later); the
`tests` data still feeds that service and the dashboard LIS.

Re-run any time:  python3 build-hospital-refdata.py
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "hospital.json")

# All 25 districts, listed in province order (this is the branch list the agent
# reads from). Provinces: Western, Central, Southern, Northern, Eastern,
# North Western, North Central, Uva, Sabaragamuwa.
BRANCHES = [
    "Colombo", "Gampaha", "Kalutara",
    "Kandy", "Matale", "Nuwara Eliya",
    "Galle", "Matara", "Hambantota",
    "Jaffna", "Kilinochchi", "Mannar", "Vavuniya", "Mullaitivu",
    "Batticaloa", "Ampara", "Trincomalee",
    "Kurunegala", "Puttalam",
    "Anuradhapura", "Polonnaruwa",
    "Badulla", "Monaragala",
    "Ratnapura", "Kegalle",
]

# Coverage partition: the 25 districts split into 5 groups of 5. Each doctor
# serves TWO adjacent groups (10 districts); per specialty we make 5 doctors,
# one per slot, so every district ends up served by exactly two doctors.
GROUPS = [
    ["Colombo", "Gampaha", "Kalutara", "Kandy", "Matale"],
    ["Nuwara Eliya", "Galle", "Matara", "Hambantota", "Jaffna"],
    ["Kilinochchi", "Mannar", "Vavuniya", "Mullaitivu", "Batticaloa"],
    ["Ampara", "Trincomalee", "Kurunegala", "Puttalam", "Anuradhapura"],
    ["Polonnaruwa", "Badulla", "Monaragala", "Ratnapura", "Kegalle"],
]

# (name, consultation fee Rs, symptom keywords find_doctor matches on)
# ORDER MATTERS: find_doctor's resolveSpecialty returns the FIRST specialty whose
# keyword matches the caller's words, so specific specialties come first and the
# broad "General Medicine" sits LAST (otherwise "child fever" / "pregnancy
# checkup" would wrongly resolve to General Medicine on the words fever/checkup).
SPECIALTIES = [
    ("Cardiology", 4500, ["heart", "chest pain", "palpitation", "palpitations", "bp", "blood pressure", "cardiac", "cardiologist", "heart attack"]),
    ("Pulmonology", 4000, ["lung", "lungs", "pulmonary", "breathing", "breath", "shortness of breath", "asthma", "wheezing", "wheeze", "chronic cough", "tb", "tuberculosis", "bronchitis", "copd", "respiratory", "chest infection"]),
    ("Neurology", 4500, ["headache", "migraine", "seizure", "fits", "nerve", "neuro", "numbness", "dizziness", "stroke", "tremor", "memory", "paralysis"]),
    ("Gastroenterology", 3800, ["stomach", "gastric", "gastritis", "acidity", "ulcer", "abdominal pain", "indigestion", "liver", "bowel", "diarrhoea", "diarrhea", "constipation", "ibs", "vomiting", "nausea"]),
    ("Nephrology", 4500, ["kidney", "renal", "nephro", "dialysis", "kidney stone", "creatinine"]),
    ("Endocrinology", 4200, ["diabetes", "sugar", "thyroid", "hormone", "endocrine", "obesity", "weight", "goitre", "insulin"]),
    ("Dermatology", 3000, ["skin", "hair", "hair loss", "hair fall", "baldness", "acne", "pimple", "rash", "eczema", "dermatologist", "scalp", "fungal", "itching"]),
    ("Orthopedics", 4000, ["bone", "joint", "knee", "fracture", "back pain", "spine", "orthopedic", "shoulder", "hip", "sprain", "ligament", "neck pain"]),
    ("Rheumatology", 3800, ["arthritis", "joint pain", "gout", "rheumatoid", "autoimmune", "lupus", "stiffness", "swollen joints"]),
    ("ENT", 3200, ["ear", "nose", "throat", "sinus", "hearing", "tonsil", "ent", "vertigo", "snoring", "voice", "sore throat"]),
    ("Ophthalmology", 3000, ["eye", "vision", "sight", "cataract", "spectacles", "glasses", "blurred vision", "red eye", "eye pain", "glaucoma"]),
    ("Dental", 2200, ["tooth", "teeth", "dental", "gum", "cavity", "toothache", "dentist", "braces", "root canal", "denture"]),
    ("Gynaecology", 3800, ["pregnancy", "pregnant", "gynae", "obstetric", "menstrual", "period", "women", "fertility", "antenatal", "menopause", "pcod"]),
    ("Pediatrics", 2800, ["child", "baby", "kid", "infant", "pediatric", "paediatric", "vaccination", "newborn", "children"]),
    ("Psychiatry", 3500, ["mental", "depression", "anxiety", "stress", "sleep", "insomnia", "psychiatry", "panic", "mood", "addiction"]),
    ("Urology", 4200, ["urine", "urinary", "prostate", "bladder", "uti", "urology", "burning urination", "frequent urination"]),
    ("General Surgery", 4000, ["surgery", "hernia", "appendix", "lump", "gallbladder", "piles", "hydrocele", "cyst", "abscess", "surgical"]),
    ("Oncology", 5000, ["cancer", "tumour", "tumor", "oncology", "chemotherapy", "malignancy", "lump breast"]),
    ("Haematology", 4200, ["blood disorder", "anaemia", "anemia", "bleeding", "clotting", "low platelets", "leukaemia", "lymphoma"]),
    # Broad catch-all — keep LAST (see ORDER MATTERS note above).
    ("General Medicine", 2500, ["fever", "cold", "cough", "checkup", "general checkup", "body ache", "weakness", "tiredness", "physician", "gp", "viral", "flu", "general"]),
]

# Reused name pool (Sinhala / Tamil / Muslim / Burgher — reflecting Sri Lanka).
NAMES = [
    "Nimal Sooriya", "Anoma Wijesinghe", "Rajiv Mehta", "Shamila Perera", "Nuwan Jayasuriya",
    "Leela Krishnan", "Hassan Aziz", "Priya Nair", "Tariq Hussain", "Chathurika Bandara",
    "Sanjeewa Fernando", "Ishara Gunasekara", "Kalana Liyanarachchi", "Sandavi Perera", "Ruwan Dissanayake",
    "Dilani Senanayake", "Pradeep Ranatunga", "Harini Wickramasinghe", "Asela Gunawardena", "Tharindu Rajapaksa",
    "Nilanthi Herath", "Kasun Abeywickrama", "Madhavi Karunaratne", "Roshan Amarasinghe", "Suresh Kumar",
    "Vasanthi Sivakumar", "Fathima Rizwan", "Imran Marikkar", "Anand Selvaratnam", "Michelle Pereira",
]

# Three clinic schedules; all include evening slots (>= 16:00) so "after 4 pm" works.
SCHED = [
    {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], "slots": ["08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "14:00", "15:00", "16:00", "16:30"]},
    {"days": ["Mon", "Wed", "Fri"], "slots": ["09:00", "09:30", "10:00", "11:00", "16:00", "16:30", "17:00", "17:30", "18:00"]},
    {"days": ["Tue", "Thu", "Sat"], "slots": ["08:00", "08:30", "09:00", "13:30", "14:00", "15:00", "16:00", "17:00", "18:00"]},
]

COMMENT = (
    "Hospital reference data. Shared by the AI APPOINTMENT voice agent "
    "(find_doctor / book_appointment) and the Hospital dashboard. Covers all 25 Sri Lankan "
    "districts (branches) and 20 specialties, with TWO doctors per district per specialty so "
    "the agent can offer a choice (mock data — doctor names and time slots are reused on "
    "purpose). 'tests' is the lab catalogue (LIS): it now feeds the SEPARATE Lab service "
    "(order_lab_test — a dormant flow activated later) and the dashboard, NOT the appointment "
    "line. confirm_test_number: while testing outbound calls, set to your mobile so EVERY "
    "hospital call rings it instead of real patients (clear to go live). Regenerate the doctor "
    "catalogue with build-hospital-refdata.py."
)


def main():
    old = open(OUT, encoding="utf-8").read()
    old_obj = json.loads(old)
    currency = old_obj.get("currency", "Rs")
    confirm = old_obj.get("confirm_test_number", "")
    # Preserve the hand-curated `tests` block verbatim (keeps its compact formatting).
    tests_text = old[old.index('"tests": ['): old.rindex("]") + 1]

    spec_lines = [
        "    " + json.dumps({"name": name, "keywords": kws}, ensure_ascii=False)
        for (name, fee, kws) in SPECIALTIES
    ]

    # Generate General Medicine doctors FIRST so an unresolved symptom + branch
    # falls back to GPs (find_doctor with no resolved specialty returns the first
    # matching doctors by branch). Resolution order (SPECIALTIES) is unaffected.
    doc_order = [SPECIALTIES[-1]] + SPECIALTIES[:-1]
    doc_lines = []
    coverage = {}
    n = 0
    for gi, (name, fee, kws) in enumerate(doc_order):
        for slot in range(5):
            n += 1
            branches = GROUPS[slot] + GROUPS[(slot + 1) % 5]
            sched = SCHED[slot % len(SCHED)]
            doc = {
                "id": f"D{n:03d}",
                "name": "Dr. " + NAMES[(gi * 5 + slot) % len(NAMES)],
                "specialty": name,
                "branches": branches,
                "fee": fee,
                "days": sched["days"],
                "slots": sched["slots"],
            }
            doc_lines.append("    " + json.dumps(doc, ensure_ascii=False))
            for b in branches:
                coverage[(b, name)] = coverage.get((b, name), 0) + 1

    # Self-check: every district x specialty must be covered by exactly 2 doctors.
    bad = []
    for b in BRANCHES:
        for (name, fee, kws) in SPECIALTIES:
            if coverage.get((b, name), 0) != 2:
                bad.append((b, name, coverage.get((b, name), 0)))
    assert not bad, f"coverage broken for: {bad[:10]}"

    parts = [
        "{",
        '  "_comment": ' + json.dumps(COMMENT, ensure_ascii=False) + ",",
        '  "currency": ' + json.dumps(currency, ensure_ascii=False) + ",",
        '  "confirm_test_number": ' + json.dumps(confirm, ensure_ascii=False) + ",",
        '  "branches": ' + json.dumps(BRANCHES, ensure_ascii=False) + ",",
        '  "specialties": [',
        ",\n".join(spec_lines),
        "  ],",
        '  "doctors": [',
        ",\n".join(doc_lines),
        "  ],",
        "  " + tests_text,
        "}",
    ]
    text = "\n".join(parts) + "\n"
    json.loads(text)  # validate before writing
    open(OUT, "w", encoding="utf-8").write(text)
    print(f"wrote {OUT}")
    print(f"  branches:    {len(BRANCHES)}")
    print(f"  specialties: {len(SPECIALTIES)}")
    print(f"  doctors:     {n}")
    print(f"  coverage:    every district x specialty has exactly 2 doctors ({len(coverage)} pairs)")


if __name__ == "__main__":
    main()
