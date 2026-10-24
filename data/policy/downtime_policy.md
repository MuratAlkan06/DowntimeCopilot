# Bayview Community Hospital: EHR Downtime Policy

Policy NUR-DT-004. Fictional document for the Downtime Copilot hackathon demo; not a real hospital policy and not for clinical use. Section numbers are stable so answers can cite them.

## 1. Activation

1.1 Downtime is declared when the electronic health record (EHR), barcode medication administration (BCMA), or computerized order entry is unavailable to a nursing unit for more than 15 minutes, or immediately when Information Security isolates the EHR (for example after a ransomware alert).

1.2 The house supervisor declares downtime by overhead page ("Code Gray, EHR downtime") and by text to all charge nurses. Each charge nurse confirms activation on their unit and records the start time on the Downtime Log (form DT-01).

1.3 On activation the charge nurse retrieves the unit downtime binder and the most recent Business Continuity Access (BCA) report for every patient. The BCA report is the last good snapshot of the record; note its timestamp on the Downtime Log. A BCA report older than 30 minutes at activation must be reconciled against the patient and the paper chart before relying on it.

1.4 Planned downtime follows the same steps, with BCA reports printed 30 minutes before the scheduled start.

## 2. Roles

2.1 Charge nurse: leads the unit response, assigns downtime forms, confirms the BCA report is current, keeps the Downtime Log, and is the single point of contact for the house supervisor.

2.2 Registered nurse (RN): documents all care on paper downtime forms, performs medication administration without scanning per section 5, and completes their own backload during recovery per section 7.

2.3 Pharmacy: verifies all new orders on paper, prepares and labels doses manually, and re-enters orders into the EHR during recovery.

2.4 Unit clerk: manages paper requisitions, labels, and transport of specimens; tracks admissions, discharges, and transfers on the census sheet; re-enters allergies, history, height, and weight during recovery.

2.5 Provider: writes or gives verbal orders per section 6 and co-signs every verbal order within 24 hours.

## 3. Documentation on paper

3.1 Use only approved downtime forms: Vitals Flowsheet (DT-10), Downtime Medication Administration Record (DT-20), Admission Note (DT-30), and Verbal Order Read-Back (DT-40). Do not document on scrap paper.

3.2 Every form page carries the patient label or, if labels cannot print, the patient name, medical record number, and bed written in full. Every entry carries the date, 24-hour time, and the initials of the person documenting.

3.3 Write legibly in black ink. Correct an error with a single line, the word "error", initials, and time. Never obliterate an entry.

3.4 Vital signs outside the unit parameters on the Vitals Flowsheet require a documented response (notification, intervention, or recheck) within 30 minutes, recorded on the same form.

3.5 Completed forms stay in the patient's downtime folder at the nurses' station until recovery. Photographs of forms taken for backload are working copies only; the paper form remains the legal record until it is scanned into the EHR.

## 4. Orders and labs

### 4.1 Routine orders and labs

4.1.1 New orders are written on the paper order sheet (DT-50) and faxed or walked to pharmacy and the laboratory. Routine labs are collected at the scheduled times on the BCA report using pre-printed downtime requisitions. The unit clerk logs each requisition on the census sheet.

### 4.2 Stat labs

4.2.1 To obtain a stat lab while the order system is down, the RN completes a red downtime requisition marked "STAT", including the patient label, the test, the ordering provider, and the collection time.

4.2.2 The RN or unit clerk calls the laboratory at extension 4-5227 to announce the stat specimen before sending it, then sends the specimen by hand delivery; do not use the pneumatic tube for stat specimens during downtime.

4.2.3 The laboratory phones critical and stat results to the unit within 60 minutes. The person receiving the result writes it on the requisition copy and reads it back to the laboratory, including the caller's name and the time.

4.2.4 The unit clerk records the stat lab and its result on the census sheet so it is included in the recovery backload.

### 4.3 Imaging and other diagnostics

4.3.1 Imaging and other diagnostics use the paper requisition and a phone call to the department; results are called to the unit.

## 5. Medications without scanning

5.1 Without barcode scanning, the RN verifies the five rights against the paper Downtime MAR (DT-20) and the most recent BCA medication list before every dose.

5.2 High-alert medications (insulin, heparin and other anticoagulants, opioids, concentrated electrolytes) require an independent double-check by a second licensed nurse before administration. Both nurses initial the Downtime MAR entry.

5.3 Check the allergy band and the BCA allergy list before every first dose. If an active medication conflicts with a documented allergy, hold the dose and contact the provider and pharmacy.

5.4 Record the exact administration time. Do not give a scheduled dose again inside its frequency window unless the Downtime MAR shows it was not given.

5.5 New medication orders received during downtime are verified by pharmacy before the first dose, except in emergencies covered by the rapid response policy.

## 6. Verbal orders and read-back

6.1 Verbal orders are limited to urgent situations where the provider cannot write the order. Telephone and verbal orders for chemotherapy are not accepted.

6.2 The receiving nurse writes the order on the Verbal Order Read-Back form (DT-40), reads the complete order back to the provider, and records "read back and confirmed", the provider name, the date, and the time.

6.3 The provider co-signs every verbal order within 24 hours. Orders without read-back or without co-sign are flagged in recovery and reviewed by the charge nurse.

## 7. Recovery and backload by role

7.1 Recovery begins when Information Services announces the EHR is restored. The charge nurse records the restore time on the Downtime Log and confirms that the unit census in the EHR matches the census sheet.

7.2 Nursing backload: each RN enters the medication administrations, vital signs, and intake and output they documented on paper during their own shift.

7.3 Pharmacy backload: pharmacy enters all orders written or received during downtime and reconciles the medication profile for every patient.

7.4 Unit backload: the unit clerk enters allergies, history, height and weight, and admissions, discharges, and transfers that occurred during downtime.

7.5 Each backloaded entry is verified against the paper form by the person entering it. High-alert medication administrations are verified by two people. Discrepancies between the restored record and the paper forms are reported to the charge nurse before the end of shift.

7.6 Paper forms are scanned into the EHR within 72 hours of recovery.

## 8. Audit

8.1 The Downtime Log, every downtime form, and the backload record are retained as part of the legal medical record.

8.2 Quality and Patient Safety reviews every downtime longer than four hours, including medication events, late or missed doses, and backload completeness.

8.3 Access to downtime data, including any local device copy, is logged by role. Device copies are encrypted and deleted after recovery is complete.
