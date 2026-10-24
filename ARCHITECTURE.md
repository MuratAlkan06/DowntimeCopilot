# Downtime Copilot Architecture

## Overview

Downtime Copilot is a two-mode application designed for hospital staff to manage critical workflows during EHR outages.

## Two Modes

### Mode 1: Normal Operation (`/`)
- **When**: EHR is online and operational
- **What**: Regular hospital app showing:
  - Patient list
  - Normal operations (standard workflow)
  - Real-time system status
- **What's hidden**: Downtime features
- **User focus**: Daily routine

### Mode 2: Downtime (`/downtime`)
- **When**: EHR goes down (system detects outage)
- **What**: Full-screen dedicated downtime workflow with:
  - **Snapshots**: Cached patient data (allergies, meds, labs, code status)
  - **Capture**: Document what happens during outage (forms, checklists, notes)
  - Ability to toggle freely between Snapshots ↔ Capture
- **User focus**: Emergency response
- **Screen real estate**: Maximized for the critical tasks

### Mode 3: Recovery (back to `/`)
- **When**: EHR systems are restored
- **What**: Normal app returns, now with recovery workflow visible:
  - Backload queue (captured records ready to re-enter)
  - Discrepancies (what changed while offline vs. snapshot)
  - Recovery status
- **User focus**: Reconciliation and verification

## Workflow

```
1. Normal Operation (/)
   ↓ [EHR Unreachable detected]
   ↓
2. Notification: "Click to enter downtime mode"
   ↓ [User clicks]
   ↓
3. Downtime Page (/downtime) - FULL SCREEN
   ├─ Snapshots Tab
   │  └─ View cached patient packets
   └─ Capture Tab
      ├─ Document notes
      ├─ Complete checklists
      ├─ Capture forms
      └─ Record verbal orders
   ↓ [Can toggle between Snapshots ↔ Capture freely]
   ↓ [EHR systems restored - automatic detection]
   ↓
4. Back to Normal Operation (/)
   ├─ Recovery Queue now visible
   ├─ Show discrepancies
   └─ Backload workflow
```

## Phase Sequencing

During downtime (`/downtime`):
- **Snapshots**: Always accessible (read-only patient data)
- **Capture**: Always accessible (document during outage)
- **Toggle freely**: User can go back and forth between tabs as needed

When systems restore:
- Auto-exit `/downtime` → Return to `/`
- **Recovery queue**: Now visible and active in normal UI
- User works through backload and reconciliation

## Key Principles

1. **Separation of concerns**: Downtime mode is a separate, dedicated experience
2. **Maximum focus**: Full-screen page for downtime = no distractions
3. **Natural workflow**: Snapshots (reference) → Capture (action) → Backload (verify)
4. **Automatic transitions**: No manual "phase changing"; the app flows with system state
5. **Staff-centric**: Designed around Priya's actual workflow during crisis

## Implementation Notes

- Downtime page is a full-screen route (`/downtime`), not a modal
- Snapshot and Capture are tabs/sections on the same page
- Notification system detects EHR status changes
- Auto-routing back to normal when `restoredAt` is set
- Recovery workflow is part of the normal UI, not the downtime page

## Why This Design

**Old design**: Three arbitrary "phases" (Go to downtime, During, Recovery) as tabs
- Confusing: didn't match actual workflow
- Not immersive: mixed with normal UI
- Poor UX: phase names didn't clarify what you're actually doing

**New design**: Two coherent modes (Normal vs. Downtime)
- Clear: matches reality (online vs. offline)
- Immersive: full-screen during crisis
- Intuitive: pages are named after what you do there (Snapshots, Capture)
- Natural flow: auto-transitions based on system state
