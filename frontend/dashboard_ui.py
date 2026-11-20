"""Presentation only: local dashboard over the existing adapter and saved artifacts."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import inspect
import re
import time

from nicegui import ui, run, background_tasks
from date_inputs import calendar_input, query_date, filter_dates
from review_catalog import filter_values, filter_selection
from chat_interactions import ENTER_HANDLER, SCROLL_SCRIPT
from chat_library_ui import render_library, confirm_delete
from verification_notifications import JobNotifications, description as verification_description
from agent_adapter import ROOT, BASE, REPORT, POLICY, DEFAULT_REQUEST, load, safe_file
from dashboard_view import (STATUS_COLORS, summary_rows, dashboard_cards, summary_presentation, shares,
                            filter_issues, display_rows, format_number, cause_label, excel_lookup, render_locations,
                            chat_route, conversation_reply, display_explanation, review_page, review_table_rows,
                            preview_display_rows, preview_number_columns, source_groups,
                            check_category, check_filter_options, preview_table_columns)

STYLE = '''
/* Original shared sizes; larger dashboard text is scoped below. */
:root {--ink:#191F28;--muted:#6B7684;--accent:#75604A;--canvas:#FFFFFF}
body {background:var(--canvas);color:var(--ink);font-family:Pretendard,"Malgun Gothic","Segoe UI",sans-serif;letter-spacing:-.3px}
body {font-size:16px}
.text-xs {font-size:14px}
.text-sm {font-size:16px}
.q-btn,.q-tab__label,.q-field,.q-item,.q-field__native,.q-field__input {font-size:16px}
.q-field__label,.q-message-name,.q-table__bottom,.q-uploader__subtitle,.q-item__label--caption,.q-field__bottom {font-size:14px}
.nicegui-markdown p,.nicegui-markdown li {font-size:16px}
.nicegui-content {padding:0}
.q-header {background:rgba(255,255,255,.97);color:var(--ink);border-bottom:1px solid #eef0f4}
.q-drawer {background:#fff!important;border-right:1px solid #edf0f4}
.brand {font-size:23px;font-weight:800;letter-spacing:-1px}
.eyebrow {font-size:14px;letter-spacing:2px;color:var(--muted);font-weight:700}
.muted {color:var(--muted);font-size:15px;line-height:1.65}
.workspace {width:100%;max-width:1560px;margin:auto;padding:28px 36px 44px;gap:22px}
.q-card {box-shadow:none;border:1px solid #EEE8E0;border-radius:20px;padding:24px;background:white}
.q-btn {border-radius:10px;text-transform:none;font-weight:600;letter-spacing:0}
.nav-button {min-height:48px;color:#4e5968;padding:10px 12px;white-space:nowrap}
.nav-button .q-btn__content {justify-content:flex-start;align-items:center;flex-wrap:nowrap;gap:12px;width:100%}
.nav-button .q-btn__content .q-icon {font-size:24px;width:24px;flex:0 0 24px;margin:0!important}
.nav-button .q-btn__content .block {white-space:nowrap;line-height:1.5}
.workspace-sidebar .muted,.workspace-sidebar .text-sm {word-break:keep-all;overflow-wrap:break-word}
.nav-button:hover {background:#F4EFE8;color:var(--accent)}
.q-tabs {background:white;border-radius:14px;padding:5px}
.q-tab {text-transform:none;min-height:44px;border-radius:10px;color:var(--muted);padding:0 17px}
.q-tab--active {color:var(--accent);background:#EAE1D5}
.q-tab__indicator {display:none}
.q-tab-panel {padding:0;overflow:visible}
.q-tab-panels {background:transparent;overflow:visible;width:100%}
.title {font-size:28px;font-weight:750;letter-spacing:-1px;line-height:1.4}
.section-title {font-size:17px;font-weight:700;letter-spacing:-.5px}
.metric-grid {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;width:100%}
.metric-card {cursor:pointer;transition:transform .18s,box-shadow .18s;min-height:170px;position:relative}
.metric-card:hover {transform:translateY(-3px);box-shadow:0 8px 24px #191f280a}
.metric-card:focus-visible {outline:3px solid #75604A}
.metric-number {font-size:38px;font-weight:750;line-height:1.2;letter-spacing:-1.5px;font-variant-numeric:tabular-nums}
.status-pill {white-space:nowrap;font-size:14px;font-weight:650;padding:4px 9px;border-radius:7px}
.cost-status {display:inline-block;white-space:nowrap;font-size:14px;font-weight:600;line-height:1.5;padding:3px 8px;border-radius:6px}
.cost-status-difference {background:#FFF0F0;color:#C9363E}
.cost-status-provisional {background:#EEE5D9;color:#75604A}
.cost-status-evidence {background:#FFF6E6;color:#986008}
.cost-status-match {background:#eaf8f2;color:#17815f}
.cost-status-list {display:flex;gap:5px;flex-wrap:wrap;margin-top:4px}
.cost-status-action {border:0;cursor:pointer;font-family:inherit}
.cost-status-action:hover {box-shadow:inset 0 0 0 1px currentColor}
.cost-status-action:focus-visible {outline:2px solid currentColor;outline-offset:2px}
.notice {background:#fff8ea;border-radius:14px;padding:17px 20px;color:#745319;width:100%}
.status-row {padding:18px 0;border-bottom:1px solid #f2f4f6;width:100%;cursor:pointer}
.status-row:last-child {border:0}
.status-row:hover {background:#F7F3ED}
.status-row:focus-visible {outline:2px solid var(--accent);outline-offset:2px}
.status-track {height:12px;border-radius:8px;overflow:hidden;display:flex;background:#f2f4f6;width:100%}
.status-track div {min-width:0}
.dashboard-analysis-grid {display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px;width:100%;align-items:stretch}
.dashboard-analysis-grid > .q-card {min-width:0}
.dashboard-analysis-grid .status-row {padding:12px 0}
.monthly-trend-content {flex:1;justify-content:flex-start;min-height:265px}
.monthly-trend-chart {flex:1;min-height:265px}
.monthly-cost-tooltip-surface {transform:none!important;transition:none!important;will-change:auto!important;left:var(--monthly-tip-left,0px)!important;top:var(--monthly-tip-top,0px)!important}
.trend-sample-badge {font-size:14px;font-weight:600;color:#75604A;background:#EEE5D9;padding:4px 9px;border-radius:6px;white-space:nowrap}
.trend-yoy {padding:10px 0 4px}
.trend-yoy-value {font-size:18px;font-weight:600;font-variant-numeric:tabular-nums;color:#75604A}
@media(max-width:1100px) {.dashboard-analysis-grid {grid-template-columns:minmax(0,1fr)}.monthly-trend-chart {flex:none;height:305px}}
.status-count {font-size:15px;font-variant-numeric:tabular-nums}
.detail-layout {display:grid;grid-template-columns:minmax(0,1fr);gap:18px;width:100%;align-items:start;container-type:inline-size}
.detail-layout.has-selection {grid-template-columns:minmax(0,1fr) 350px}
.detail-pane {position:sticky;top:90px;max-height:calc(100vh - 110px);overflow:auto;gap:16px}
.detail-pane .q-expansion-item {width:100%;background:#FAF8F5;border-radius:10px}
.detail-pane .q-item {padding:10px 12px;font-size:15px}
.detail-pane .q-expansion-item__content {padding:0 12px 12px}
.value-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;width:100%;align-items:stretch}
.value-cell {display:flex;flex-direction:column;min-width:0;background:#FAF8F5;border-radius:10px;padding:10px 8px;gap:8px}
.value-cell .value-label {min-height:40px;line-height:20px;word-break:keep-all;overflow-wrap:normal}
.value-cell .number {margin-top:auto;line-height:22px;font-size:16px;font-weight:700;font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.q-table__container {box-shadow:none;border:0;border-radius:12px}
.q-table thead tr {background:#FAF8F5}
.q-table th {color:var(--muted);font-weight:600;font-size:15px;border-bottom:0!important;height:44px}
.q-table tbody td {font-size:16px;border-bottom:1px solid #f4f6f9!important;height:54px;max-width:235px;white-space:normal!important;overflow-wrap:anywhere}
.q-table tbody tr {cursor:pointer}
.q-table tbody tr.selected {background:#EAE1D5!important}
.q-table tbody tr:hover {background:#F4EFE8}
.review-table .q-table tbody tr.review-active-row > td {background:#F1E8DC!important}
.review-table .q-table tbody tr.review-active-row > td:first-child {box-shadow:inset 3px 0 #75604A}
.q-table .text-right {font-variant-numeric:tabular-nums;white-space:nowrap!important}
.q-table__bottom {color:var(--muted);border-top:0}
.source-preview-table .q-table {table-layout:fixed;min-width:var(--preview-min-width);width:100%}
.source-preview-dialog {width:min(1440px,96vw);max-width:96vw!important;max-height:88vh!important;overflow:hidden;display:flex;flex-direction:column;padding:24px!important;gap:12px!important}
.source-preview-body {flex:1;overflow:hidden;gap:10px!important}
.source-preview-table {min-height:0;max-height:62vh}
.source-preview-table .q-table__middle {overflow:auto;max-height:56vh}
.source-preview-table .q-table th {position:sticky;top:0;z-index:1;background:#faf8f5}
.source-preview-table .q-table th,.source-preview-table .q-table tbody td {padding:10px 12px;vertical-align:middle;line-height:1.4;height:44px}
.source-preview-table .preview-cell-text {display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;line-height:22px}
.source-preview-table .q-table th,.source-preview-table .q-table tbody td.preview-value {white-space:nowrap!important;overflow-wrap:normal;word-break:normal;overflow:hidden;text-overflow:ellipsis}
.source-preview-table .q-table tbody td.preview-description {white-space:nowrap!important;overflow:hidden;text-overflow:ellipsis}
.review-table .q-table__bottom .q-btn {display:none}
.review-table {min-width:0}
.review-table .q-table {table-layout:fixed;width:max(var(--review-table-min-width),calc(100cqw - 48px));min-width:var(--review-table-min-width)}
.review-table .q-table th,.review-table .q-table tbody td {padding:12px 10px;vertical-align:middle;max-width:none;white-space:nowrap!important;overflow:hidden;text-overflow:ellipsis;line-height:1.45}
.review-table .q-table tbody td {height:88px}
.review-table .q-table .review-status-cell {padding-right:24px}
.review-table .q-table .review-handler-cell {padding-left:20px}
.review-status-cell .status-pill {display:inline-flex;align-items:center;line-height:1.4}
.review-table.has-review-selection .q-table th:first-child,.review-table.has-review-selection .q-table tbody td:first-child {width:56px;min-width:56px;max-width:56px;padding:8px;text-align:center;overflow:visible}
.review-table.has-review-selection .q-checkbox {vertical-align:middle;margin:0}
.review-cell-title,.review-cell-location {display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;white-space:normal;word-break:keep-all;overflow-wrap:anywhere;line-height:1.45}
.review-cell-title {font-weight:600}
.review-cell-meta {font-size:14px;color:var(--muted);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.review-cell-location {color:var(--accent)}
.q-field--outlined .q-field__control {border-radius:10px;background:#FCFBFA}
.q-field--outlined .q-field__control:before {border-color:#e9edf2}
.q-field__label {font-size:15px}
.compact-date-calendar .q-date__view {min-height:270px;padding:8px}
.compact-date-calendar .q-date__navigation,.compact-date-calendar .q-date__calendar-weekdays {height:30px}
.compact-date-calendar .q-date__navigation .q-btn {min-height:28px;padding:0 4px}
.nicegui-markdown {font-size:15px;line-height:1.9;overflow:auto;max-width:100%;width:100%}
.nicegui-markdown th {background:#FAF8F5}
.nicegui-markdown td {vertical-align:top;min-width:90px}
.report-section {background:white;border-radius:14px;width:100%;overflow:hidden}
.report-section .q-item {padding:18px 24px;font-weight:600}
.report-section .q-expansion-item__content {padding:0 24px 24px}
.report-dialog {width:min(1480px,96vw);max-width:96vw!important;max-height:94vh;overflow:auto;padding:24px;gap:14px}
.report-review-cell,.report-amount-cell {white-space:nowrap!important;font-variant-numeric:tabular-nums}
.report-review-amount {font-weight:600;color:#75604A}
.report-money-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;width:100%;align-items:stretch}
.report-money-card {display:grid;grid-template-rows:auto auto 1fr;align-content:start;gap:8px;background:#FAF8F5;padding:16px 18px;border-radius:12px;min-width:0}
.report-money-title {font-size:16px;font-weight:500;line-height:1.4}
.report-money-caption {font-size:14px;line-height:1.5;color:#6B7684}
.report-money-card > :last-child {align-self:end}
.report-brief {padding:12px 16px;background:#FAF9F7;border-left:3px solid #D6C8B7;border-radius:0 6px 6px 0}
.report-brief-result {font-size:16px;color:#29313B;line-height:1.7}
.report-money-value {font-size:32px;font-weight:650;letter-spacing:-.6px;font-variant-numeric:tabular-nums;white-space:normal}
.report-money-value .money-number {line-height:1.25}
.report-money-value .money-unit {font-size:16px;font-weight:400;line-height:1.4}
.report-boundary {background:#FFF8EA;border-radius:10px;padding:10px 14px;font-size:15px;color:#745319;width:100%}
.report-summary-table {min-width:0;max-width:100%}
.report-summary-table .q-table {table-layout:fixed;width:100%;min-width:1180px}
.report-summary-table .q-table__middle {overflow-x:auto}
.report-summary-table .q-table tbody td {height:72px;font-size:16px;vertical-align:middle;line-height:1.5}
.report-summary-table .q-table th,.report-summary-table .q-table td {padding:14px 12px}
.report-summary-table .q-table th:nth-child(1) {width:14%}
.report-summary-table .q-table th:nth-child(2),.report-summary-table .q-table th:nth-child(3),.report-summary-table .q-table th:nth-child(5),.report-summary-table .q-table th:nth-child(6) {width:14%}
.report-summary-table .q-table th:nth-child(4) {width:11%}
.report-summary-table .q-table th:nth-child(7) {width:19%}
.report-summary-table .report-status-cell {white-space:normal;word-break:keep-all}
.report-summary-table .cost-status-list {gap:6px;margin-top:6px}
.report-summary-table .q-table td:first-child {font-weight:600}
.checklist-actions {margin-left:auto;justify-content:flex-end;gap:8px}
.financial-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;width:100%;align-items:stretch}
.financial-main-card {padding:18px;gap:8px;border-radius:18px;min-width:0}
.financial-card-heading {min-height:24px}
.financial-headline {display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
.financial-tax-row {margin-top:auto;padding-top:10px;border-top:1px solid #EAE5DE}
.financial-tax-value {font-size:18px;line-height:1.5;font-variant-numeric:tabular-nums}
.financial-main-card .section-title,.billed-detail-card .section-title {font-size:15px;font-weight:600}
.money-display {display:flex;align-items:baseline;gap:5px;white-space:normal;color:#29313B;min-width:0}
.money-number {font-family:"Segoe UI",Arial,sans-serif;font-variant-numeric:tabular-nums;line-height:1.4;word-break:keep-all;overflow-wrap:anywhere}
.money-unit {font-size:15px;font-weight:400;letter-spacing:0;color:#737C86}
.financial-value {font-size:32px;font-weight:750;letter-spacing:-.8px;margin:3px 0;line-height:1.35;color:#191F28;white-space:normal;font-variant-numeric:tabular-nums}
.financial-review {background:#FCFBF9}
.financial-report-link {cursor:pointer;transition:background .15s,border-color .15s}
.financial-report-link:hover {background:#F8F5F0;border-color:#C6B5A1}
.financial-report-link:focus-visible {outline:2px solid #75604A;outline-offset:3px}
.financial-review .financial-value {color:var(--accent)}
.financial-share {margin:0;padding:0}
.financial-ratio {color:#75604A;font-size:26px;line-height:1.2;font-weight:650;letter-spacing:-.3px;font-variant-numeric:tabular-nums}
.financial-track {height:10px;border-radius:6px;background:#EAE1D5;width:100%;overflow:hidden}
.billed-detail-card {padding:18px 20px;border-radius:14px}
.billed-breakdown {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));width:100%;gap:8px;margin:4px 0}
.billed-breakdown-row {display:grid;grid-template-columns:minmax(0,1fr) auto 16px;align-items:center;min-width:0;gap:10px;padding:14px 12px;border-radius:6px;background:#FAF9F7;cursor:pointer;transition:background .15s}
.billed-breakdown-row:hover {background:#F0E8DE}
.billed-breakdown-row:focus-visible {outline:2px solid var(--accent);outline-offset:2px}
.cost-detail-dialog {width:min(1180px,96vw);max-width:96vw!important;max-height:90vh;overflow:auto;gap:16px}
.cost-items-table .q-table {table-layout:fixed;width:100%;min-width:1020px}
.cost-items-table .q-table th,.cost-items-table .q-table tbody td {padding:12px 10px;vertical-align:middle}
.cost-items-table .q-table tbody td {max-width:none;line-height:1.5;word-break:keep-all;overflow-wrap:normal}
.cost-items-table .q-table tbody td.cost-col-connection,.cost-items-table .q-table tbody td.cost-col-comparison_result,.cost-items-table .q-table tbody td.cost-col-location,.cost-items-table .q-table tbody td.text-right {white-space:nowrap!important}
.cost-items-table .cost-col-connection .muted {font-size:inherit;line-height:inherit}
.cost-items-table .cost-source-link {padding:0;min-height:0;font-size:inherit;line-height:inherit;font-weight:500}
.cost-items-table .cost-source-link .q-btn__content {justify-content:flex-start;flex-wrap:nowrap;white-space:nowrap}
.cost-source-preview {background:#FAF8F5;border-radius:12px;padding:16px}
.billed-breakdown-label {color:#515B66;font-size:15px;line-height:1.4}
.billed-breakdown-value {font-size:19px;line-height:1.4;font-weight:500;letter-spacing:-.4px;gap:3px}
.billed-breakdown-value .money-unit {font-size:15px}
.billed-breakdown-chevron {color:#A5A09A}
.completion-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;width:100%}
.completion-card {cursor:pointer}
.completion-count {display:flex;align-items:baseline;gap:7px;white-space:nowrap}
.completion-count-unit {color:#6B7684;font-size:15px;line-height:1.5}
.completion-money-sentence {display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 7px;width:100%;padding:4px 0 0;font-variant-numeric:tabular-nums}
.completion-money-total {color:#6B7684;font-size:15px;line-height:1.5;white-space:nowrap}
.completion-money-separator {color:#ADB3BA;font-size:15px}
.completion-money-pending {color:var(--accent);font-size:16px;font-weight:600;line-height:1.5;white-space:nowrap}
.completion-track {height:8px;width:100%;background:#EAE1D5;border-radius:5px;overflow:hidden;flex:none}
.cost-composition-card {gap:12px;padding:20px}
.cost-composition-caption {font-size:14px;color:#6B7684;line-height:1.4}
.cost-composition-track {height:22px;width:100%;display:flex;flex:none;background:#F2EEE8;border-radius:6px;overflow:hidden;min-width:0}
.cost-composition-fill {height:100%;flex-shrink:0}
.cost-composition-list {width:100%;gap:0}
.cost-composition-row {display:grid;grid-template-columns:10px minmax(0,1fr) auto 16px;gap:8px 10px;align-content:center;align-items:center;width:100%;padding:12px 0;border-bottom:1px solid #F1EDE7;cursor:pointer}
.cost-composition-share {font-size:15px;line-height:1.4;text-align:right;color:#6B7684;font-variant-numeric:tabular-nums}
.cost-composition-row:last-child {border-bottom:none}
.cost-composition-row:hover {background:#FAF8F5}
.cost-composition-row:focus-visible {outline:2px solid var(--accent);outline-offset:2px}
.cost-composition-swatch {width:10px;height:10px;border-radius:3px;box-shadow:inset 0 0 0 1px rgba(82,59,45,.12)}
.cost-composition-label {font-size:18px;font-weight:500;word-break:keep-all}
.cost-composition-value {font-size:21px;font-weight:600;white-space:nowrap}
.cost-composition-value .money-number {white-space:nowrap}
.dashboard-analysis-grid .monthly-trend-chart {height:300px;min-height:300px;flex:none}
.completion-ratio {font-size:20px;line-height:1.25;font-weight:600;color:#75604A;letter-spacing:-.4px;font-variant-numeric:tabular-nums}
.completion-card:hover {background:#FAF8F5}
.completion-card:focus-visible {outline:2px solid var(--accent)}
@media(max-width:1100px) {.billed-breakdown {grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:760px) {.report-money-grid,.financial-grid,.completion-grid {grid-template-columns:1fr}.report-money-value,.financial-value {font-size:28px}.financial-ratio {font-size:18px}.report-dialog {padding:16px}}
@media(max-width:480px) {.billed-breakdown {grid-template-columns:1fr}}
.chat-content {max-width:880px;margin:auto;width:100%;padding:12px 0 185px}
.chat-messages {gap:18px;overflow-anchor:none}
.q-message {margin-bottom:0}
.bg-chat-sent {background:#EAE1D5!important}
.text-chat-sent {color:#EAE1D5!important}
.bg-chat-received {background:#F0F3F2!important}
.text-chat-received {color:#F0F3F2!important}
.chat-entry {width:100%;position:relative}
.message-actions {opacity:0;transition:opacity .15s;margin-bottom:2px}
.chat-entry:hover .message-actions,.chat-entry:focus-within .message-actions,.selecting .message-actions {opacity:1}
.chat-content:not(.selecting) .message-selector {display:none}
@media (hover:none) {.message-actions {opacity:1}}
.chat-new {animation:chat-arrive .22s ease-out both}
@keyframes chat-arrive {from {opacity:0;transform:translateY(7px)} to {opacity:1;transform:translateY(0)}}
@media (prefers-reduced-motion:reduce) {.chat-new {animation:none}}
.q-message-text:before {display:none}
.latest-message {position:absolute;bottom:100%;left:50%;transform:translateX(-50%);background:white!important;box-shadow:0 2px 12px #191f2810;margin-bottom:8px}
.q-message-name {font-size:15px;color:var(--muted);margin-bottom:7px}
.q-message-text {border-radius:16px;padding:15px 20px;line-height:1.9;box-shadow:none}
.q-message-text-content {white-space:pre-wrap;overflow-wrap:anywhere;font-size:16px}
.composer-dock {position:fixed;bottom:0;left:240px;right:0;padding:15px 32px 18px;background:linear-gradient(transparent,var(--canvas) 18%);z-index:30}
.composer {max-width:880px;margin:auto;background:white;border:1px solid #E8E0D6;border-radius:18px;padding:10px 14px;box-shadow:0 6px 24px #191f2806}
.composer textarea {max-height:150px;overflow:auto!important}
.composer-note {max-width:880px;margin:7px auto 0;font-size:14px;color:var(--muted)}
.chat-target-chip {max-width:880px;margin:0 auto 8px;padding:7px 11px;border-radius:12px;background:#F4EFE8;width:100%;font-size:15px}
.command-card {border:1px solid #E8E0D6;gap:14px;box-shadow:none!important}
.command-target {background:#FAF8F5;border-radius:10px;padding:10px 12px;width:100%}
.command-target .muted {font-size:14px}
.q-uploader {box-shadow:none;border:1px dashed #D8CCBC;border-radius:14px}
@media (max-width:1250px) {.workspace {padding:24px}.detail-layout.has-selection {grid-template-columns:minmax(0,1fr)}.detail-pane {position:static;max-height:none}.metric-card {padding:20px}.metric-number {font-size:32px}}
@media (max-width:900px) {.composer-dock {left:0}.metric-grid {grid-template-columns:repeat(2,minmax(0,1fr))}.workspace {padding:20px}.title {font-size:25px}}
@media (max-width:550px) {.workspace {padding:16px 12px}.q-card {padding:18px;border-radius:16px}.metric-card {min-height:150px}.metric-number {font-size:30px}.q-tab {padding:0 12px;min-width:85px}.composer-dock {padding:12px}.detail-layout {display:block}.detail-pane {margin-top:16px}.header-title {font-size:15px!important}.q-table tbody td {min-width:75px}.notice {padding:15px}.scope-line {font-size:15px}}

/* Enlarge only the dashboard. Other pages, navigation and dialogs retain their original sizes. */
.dashboard-page {font-size:18px}
.dashboard-page .text-xs,.dashboard-page .status-pill,.dashboard-page .cost-status {font-size:16px}
.dashboard-page .text-sm,.dashboard-page .q-btn {font-size:18px}
.dashboard-page .muted,.dashboard-page .status-count,.dashboard-page .money-unit,.dashboard-page .billed-breakdown-label {font-size:17px}
.dashboard-page .title {font-size:30px}
.dashboard-page .section-title {font-size:19px}
.dashboard-page .financial-main-card .section-title,.dashboard-page .billed-detail-card .section-title {font-size:17px}
.dashboard-page .metric-number {font-size:40px}
.dashboard-page .financial-value {font-size:34px}
.dashboard-page .financial-ratio {font-size:28px}
.dashboard-page .billed-breakdown-value {font-size:21px}
.dashboard-page .completion-ratio {font-size:22px}
@media(max-width:1250px) {.dashboard-page .metric-number {font-size:34px}}
@media(max-width:900px) {.dashboard-page .title {font-size:27px}}
@media(max-width:760px) {.dashboard-page .financial-value {font-size:30px}.dashboard-page .financial-ratio {font-size:20px}}
@media(max-width:550px) {.dashboard-page .metric-number {font-size:32px}.dashboard-page .scope-line {font-size:17px}}

/* Keep the 8082 reference layout independent of the current dashboard. */
.dashboard-classic .dashboard-analysis-grid .monthly-trend-chart {height:420px;min-height:420px;flex:1}
.dashboard-classic .status-count {font-size:14px}
.dashboard-current {font-size:16px}
.dashboard-current .title {font-size:28px;line-height:1.35}
.dashboard-current .section-title {font-size:18px}
.dashboard-current .text-sm,.dashboard-current .q-btn {font-size:16px}
.dashboard-current .text-xs,.dashboard-current .status-pill,.dashboard-current .cost-status {font-size:14px}
.dashboard-current .muted,.dashboard-current .money-unit {font-size:15px;line-height:1.6}
.dashboard-current .financial-main-card .section-title {font-size:16px}
.dashboard-current .financial-value {font-size:32px;line-height:1.3}
.dashboard-current .financial-ratio {font-size:24px}
.dashboard-current .completion-number {font-size:28px;line-height:1.2}
.dashboard-current .completion-ratio {font-size:20px}
.dashboard-current .completion-card {padding:18px 20px}
.dashboard-current .cost-composition-card {align-self:stretch}
.dashboard-current .cost-composition-list {flex:1;justify-content:flex-start}
.dashboard-current .cost-composition-list .cost-composition-row {flex:1;min-height:54px}
.dashboard-current .cost-composition-list .cost-composition-credit {flex:0;min-height:54px}
.dashboard-current .cost-composition-card .section-title {font-size:19px}
.dashboard-current .cost-composition-card .muted,.dashboard-current .cost-composition-card .money-unit {font-size:17px;line-height:1.5}
.dashboard-current .cost-composition-label {font-size:18px}
.dashboard-current .cost-composition-value {font-size:21px}
.dashboard-current .trend-yoy-value {font-size:17px}
.dashboard-current .dashboard-analysis-grid .monthly-trend-chart {height:420px;min-height:420px;flex:none}
.coverage-collapsible {padding:0!important;overflow:hidden}
.coverage-expansion > .q-expansion-item__container > .q-item {padding:18px 20px;min-height:60px;font-size:17px;font-weight:600}
.coverage-content {padding:0 20px 20px;gap:12px}
@media(max-width:760px) {
    .dashboard-current .title {font-size:25px}
    .dashboard-current .financial-value {font-size:30px}
    .dashboard-current .dashboard-analysis-grid .monthly-trend-chart,.dashboard-classic .dashboard-analysis-grid .monthly-trend-chart {height:340px;min-height:340px}
}
'''


def render_page(adapter, download_url, web_report, evidence_form, *, dashboard_variant=None):
    ui.colors(primary='#75604A', secondary='#6B7684', positive='#1A9D72', negative='#E5484D', warning='#D99013')
    ui.add_css(STYLE)
    if dashboard_variant == 'priority':
        from priority_dashboard import STYLE as PRIORITY_STYLE
        ui.add_css(PRIORITY_STYLE)
    ui.page_title('DEMO COMPANY · 물류비 검증')
    policy = load(ROOT / POLICY)
    period = policy.get('period', {})
    scope = f"가상센터 · 가상물류사 · {policy.get('invoice_month', '기간 미등록')} 청구"
    period_text = f"비교기간 {period.get('start', '미등록')} ~ {period.get('end', '미등록')}"
    last = {'signature': None, 'files': None, 'questions': None, 'intake': None, 'memory': None, 'disposition': None, 'rule': None}
    view = {'rows': [], 'group': '전체', 'status': '전체', 'query': '', 'selected': None, 'focus_ids':None, 'cost_context':None}
    checklist_page = {'rows': [], 'lookup': {}, 'syncing': False}
    nav_buttons = {}
    rendered_messages = []
    selected_messages = set()
    message_selection_mode = {'active': False}
    message_checkboxes = {}
    last_library = {'revision':None,'chat_id':None}
    last_submit = {'text': '', 'at': 0.0}
    target_action = {'button': None}
    memory_ui = {'busy': False, 'message': ''}
    memory_supported = all(hasattr(adapter, name) for name in
                           ('prepare_memory_preview', 'confirm_memory_preview', 'cancel_memory_preview', 'saved_memory'))
    disposition_supported = all(hasattr(adapter, name) for name in
                                ('prepare_disposition', 'confirm_disposition', 'cancel_disposition'))
    disposition_ui = {'busy': False, 'message': ''}
    rule_supported = all(hasattr(adapter, name) for name in
                         ('rule_options', 'prepare_rule', 'confirm_rule', 'cancel_rule'))
    rule_ui = {'busy': False, 'message': ''}
    checklist_ui={'busy':False}
    bulk_supported=hasattr(adapter,'complete_checklist')
    job_notifications=JobNotifications()
    banner_state={'signature':None}
    rule_labels = {'order_linked':'주문 연결','shipment_linked':'송장 연결','match':'일치', 'difference':'불일치', 'not_determinable':'판단 불가',
                   'excluded':'비교 제외', 'review_required':'기준 재확인'}
    review_labels = {'unreviewed': '미처리', 'reviewed': '검토완료',
                     'exception_accepted': '예외인정', 'review_required': '미처리'}

    with ui.left_drawer(value=True).props('width=240 breakpoint=900').classes('workspace-sidebar px-4 py-7') as drawer:
        with ui.row().classes('items-center gap-3 mb-1'):
            with ui.element('div').style('background:#75604A;border-radius:10px;padding:8px;display:flex'):
                ui.icon('fact_check', size='22px', color='white')
            ui.label('DEMO COMPANY').classes('brand')
        ui.label('LOGISTICS WORKSPACE').classes('eyebrow mt-3 mb-9')
        for name, icon, target in [('대시보드', 'space_dashboard', '대시보드'), 
                                    ('확인 목록', 'rule', '확인 목록'),
                                    ]:
            nav_buttons[target] = ui.button(name, icon=icon, on_click=lambda t=target: tabs.set_value(t)).props('flat no-caps align=left color=grey-7').classes('nav-button w-full mb-1')
        ui.separator().classes('my-6')
        ui.label('가상 자료 체험 · AI 미사용').classes('muted')
        ui.space()
        with ui.column().classes('gap-2 mt-8'):
            ui.label('기존 완료 결과').classes('muted')
            ui.label(scope).classes('text-sm font-semibold')
            ui.label(period_text).classes('muted')
            ui.label('포함된 가상 자료 계산·조회').classes('muted')
            ui.label('웹에서 바로 사용 · 설치 불필요').classes('muted mt-4')
    with ui.header().classes('px-5 py-3 items-center justify-between'):
        with ui.row().classes('items-center gap-3'):
            ui.button(icon='menu', on_click=drawer.toggle).props('flat round dense').classes('lg:hidden')
            ui.label('물류비 청구 검증 에이전트').classes('header-title text-base font-semibold')

    with ui.column().classes('workspace'):
        with ui.card().classes('w-full gap-2'):
            ui.label('가상 청구·WMS 검증 체험').classes('text-lg font-semibold')
            ui.label('포함된 가상 자료를 실제 계산합니다. AI 대화와 개인 파일 등록은 사용하지 않습니다.').classes('muted')
            def calculate_sample():
                try:adapter.start('public_calculate')
                except ValueError as exc:ui.notify(str(exc),type='warning')
            calc_button=ui.button('가상 자료 다시 계산',icon='calculate',on_click=calculate_sample).props('no-caps data-testid=public-calculate')
            calc_status=ui.label(adapter.calculation_status()).props('data-testid=public-calculation-status')
            def show_calculation_status():
                calc_status.set_text(adapter.calculation_status())
                calc_button.set_enabled(adapter.snapshot()['verification_job']['status']!='running')
            ui.timer(1,show_calculation_status)
        verification_banner=ui.column().classes('w-full gap-1').props('data-testid=verification-banner')
        with ui.tabs().classes('w-full justify-start').props('align=left') as tabs:
            dashboard_tab = ui.tab('대시보드')
            chat_tab = ui.tab('대화').style('display:none')
            results_tab = ui.tab('확인 목록')
            evidence_tab = ui.tab('추가 증빙').style('display:none')
            monthly_tab = ui.tab('월별 검증').style('display:none')
            library_tab = ui.tab('채팅 보관함').style('display:none')
        if dashboard_variant == 'priority':
            tabs.style('display:none')
        with ui.tab_panels(tabs, value=dashboard_tab, animated=False):
            with ui.tab_panel(dashboard_tab).classes('dashboard-page ' + ('dashboard-classic' if dashboard_variant == 'classic' else 'dashboard-current' if dashboard_variant is None else '')):
                dashboard = ui.column().classes('w-full gap-6')
            with ui.tab_panel(results_tab):
                with ui.column().classes('w-full gap-5'):
                    with ui.row().classes('w-full items-center justify-between gap-3'):
                        with ui.column().classes('gap-1'):
                            ui.label('확인이 필요한 내역').classes('title')
                        with ui.row().classes('checklist-actions items-center').props('data-testid=checklist-actions'):
                            ui.button('CSV 다운로드', icon='download', on_click=lambda: ui.download(download_url(BASE / '검토필요_전체목록.csv'))).props('outline no-caps').tooltip('계산 결과의 확인 목록 다운로드')
                            if disposition_supported:
                                ui.button('처리 상태 포함 CSV', icon='download', on_click=lambda: ui.download('/review-status.csv')).props('outline no-caps data-testid=review-csv-download').tooltip('담당자의 완료·취소 상태를 포함한 목록 다운로드')
                            if hasattr(adapter,'rate_catalog'):
                                ui.button('청구 단가 비교',icon='price_change',on_click=lambda:rate_dialog()).props('outline no-caps data-testid=rate-catalog-open').tooltip('선택한 단가로 청구금액 다시 비교')
                    with ui.card().classes('w-full gap-4'):
                        with ui.row().classes('w-full items-center gap-3'):
                            category = ui.select(['전체'], value=['전체'], label='업무 구분', multiple=True).props('outlined dense data-testid=review-category-filter').classes('w-56 max-w-full')
                            group = ui.select(['전체'], value=['전체'], label='검증 항목', multiple=True, with_input=True).props('outlined dense data-testid=review-check-filter').classes('w-72 max-w-full')
                            group.set_visibility(False)
                            status_filter = ui.select({'확인 필요':'확인 필요','전체':'전체','정상':'일치·정상','불일치':'불일치','미검증':'미검증','잠정 확정':'잠정 확정','직접 완료':'직접 완료'}, value=['확인 필요'], label='상태', multiple=True).props('outlined dense data-testid=review-status-filter').classes('w-64 max-w-full')
                            query = ui.input('주문번호·품번·원인 검색').props('outlined dense clearable debounce=250').classes('grow min-w-[200px]')
                            ui.button('초기화', on_click=lambda: select_filter()).props('flat no-caps')
                        with ui.row().classes('w-full items-center gap-4'):
                            date_from = calendar_input('조회 시작일', placeholder='YYYY-MM-DD', default_date=period.get('start'), compact=True).props('dense clearable debounce=350 data-testid=review-date-from').classes('w-48 max-w-full')
                            date_to = calendar_input('조회 종료일', placeholder='YYYY-MM-DD', default_date=period.get('end'), compact=True).props('dense clearable debounce=350 data-testid=review-date-to').classes('w-48 max-w-full')
                            date_from.without_auto_validation()
                            date_to.without_auto_validation()
                        with ui.row().classes('w-full items-center gap-3').props('data-testid=cost-check-context') as cost_context_bar:
                            cost_context_label = ui.label('').classes('font-semibold')
                            context_normal = ui.checkbox('일치 포함', value=False).props('data-testid=cost-context-normal')
                            ui.button('비용 범위 해제', on_click=lambda: select_filter()).props('flat dense no-caps')
                        cost_context_bar.set_visibility(False)
                        count_label = ui.label('').classes('muted')
                        with ui.row().classes('w-full items-center justify-between gap-2') as bulk_bar:
                            with ui.row().classes('items-center gap-2'):
                                bulk_count=ui.label('선택 0개').classes('muted')
                                ui.button('검색 결과 전체 선택',on_click=lambda:select_all_review()).props('flat no-caps data-testid=select-all-review')
                                ui.button('선택 해제',on_click=lambda:clear_review_selection()).props('flat no-caps')
                            with ui.row().classes('items-center justify-end gap-2 ml-auto').props('data-testid=bulk-review-actions'):
                                bulk_done=ui.button('선택 항목 처리완료',icon='check_circle',on_click=lambda:bulk_review('reviewed')).props('unelevated no-caps data-testid=bulk-review-done')
                                bulk_reset=ui.button('완료 취소',on_click=lambda:bulk_review('reset')).props('flat no-caps data-testid=bulk-review-reset').tooltip('처리완료를 취소하고 원래 계산 판정으로 되돌립니다.')
                        bulk_bar.set_visibility(bulk_supported)
                    with ui.element('div').classes('detail-layout') as detail_layout:
                        with ui.card().classes('w-full min-w-0 p-2'):
                            cols = []
                            for field, label in [('_display_group','검증 항목'),('결과','상태'),('_display_review','담당자 처리'),('_display_target','확인 대상'),('일자','일자'),
                                                 ('청구값','청구값'),('비교값','비교값'),('차이','차이'),('단위','단위')]:
                                numeric = field in ('청구값','비교값','차이')
                                col = {'name':field,'label':label,'field':field,'align':'right' if numeric else 'left','sortable':True}
                                if numeric:
                                    col[':sort'] = '(a,b) => Number(String(a).replaceAll(",","")) - Number(String(b).replaceAll(",",""))'
                                cols.append(col)
                            if rule_supported:
                                cols.append({'name':'_rule_status','label':'새 기준 비교','field':'_rule_status','align':'left'})
                            widths = {'_display_group':164, '결과':116, '_display_review':144,
                                      '_display_target':240, '일자':138, '청구값':120,
                                      '비교값':120, '차이':112, '단위':100, '_rule_status':116}
                            for col in cols:
                                col['headerStyle'] = f"width:{widths[col['name']]}px"
                                if col['name']=='_display_review':
                                    col['headerStyle']+=';padding-left:20px'
                            table = ui.table(columns=cols, rows=[], row_key='_id', selection='multiple' if bulk_supported else None, pagination={'rowsPerPage':10, 'page':1, 'rowsNumber':0}).classes('w-full review-table').props('flat data-testid=issue-table separator=horizontal rows-per-page-label="페이지당 행" no-data-label="일치하는 내역이 없습니다" :rows-per-page-options="[10,20,50,100]"')
                            if bulk_supported:
                                table.classes('has-review-selection')
                                table.add_slot('header-selection', '<q-checkbox dense v-model="props.selected" @click.stop />')
                                table.add_slot('body-selection', '<q-checkbox dense v-model="props.selected" @click.stop />')
                            table.style(f"--review-table-min-width:{sum(widths[col['name']] for col in cols) + (56 if bulk_supported else 0)}px")
                            def selection_changed(e):
                                table.selected=[{'_id':key} for key in dict.fromkeys(r['_id'] for r in e.selection)]
                                bulk_count.set_text(f'선택 {len(table.selected):,}개')
                            table.on_select(selection_changed)
                            if rule_supported:
                                table.add_slot('body-cell-_rule_status', '''<q-td :props="props"><span style="color:#75604A;font-weight:600">{{ ({order_linked:'주문 연결',shipment_linked:'송장 연결',match:'일치',difference:'불일치',not_determinable:'판단 불가',excluded:'비교 제외',review_required:'기준 재확인'})[props.value] || props.value || '—' }}</span></q-td>''')
                            for field in ('청구값', '비교값', '차이'):
                                table.add_slot('body-cell-' + field, '''<q-td :props="props" :style="props.col.name === '차이' && Number(String(props.row['차이']).replaceAll(',','')) !== 0 && props.row['차이'] ? 'color:#E5484D;font-weight:700' : ''">{{ props.row['_display_' + props.col.name] }}</q-td>''')
                            table.add_slot('body-cell-결과', '''<q-td :props="props" class="review-status-cell"><span class="status-pill" :style="(props.row._effective_status || props.value) === '불일치' ? 'background:#fff0f0;color:#d83a40' : (props.row._effective_status || props.value) === '미검증' ? 'background:#fff6e6;color:#a86a08' : (props.row._effective_status || props.value) === '잠정 확정' ? 'background:#EEE5D9;color:#75604A' : 'background:#eaf8f2;color:#17815f'">{{ props.row._display_status || props.row._effective_status || props.value }}<q-tooltip v-if="props.row._review_status === 'reviewed' || props.row._review_status === 'exception_accepted'">담당자가 처리완료한 내역입니다. 원래 계산 결과는 상세에서 확인할 수 있습니다.</q-tooltip><q-tooltip v-else-if="props.row._display_status === '일치'">수행한 계산·대사가 일치합니다. 실제 작업·계약 확인 여부는 상세에서 확인할 수 있습니다.</q-tooltip></span></q-td>''')
                            table.add_slot('body-cell-_display_review', '''<q-td :props="props" class="review-handler-cell"><span style="color:#6B7684">{{ props.value || '미처리' }}</span></q-td>''')
                            table.add_slot('body-cell-_display_group', '''<q-td :props="props"><span :title="props.value">{{ props.value }}</span></q-td>''')
                            table.add_slot('body-cell-_display_target', '''<q-td :props="props"><div class="review-cell-title" :title="props.row._display_target">{{ props.row._display_target }}</div><div class="review-cell-meta" :title="props.row._display_target_kind">{{ props.row._display_target_kind }}</div></q-td>''')
                            table.add_slot('body-cell-일자', '''<q-td :props="props" style="white-space:nowrap">{{ props.value && props.value !== '미기재' ? props.value : '—' }}<q-tooltip v-if="!props.value || props.value === '미기재'">원본에 일자가 기재되어 있지 않습니다.</q-tooltip></q-td>''')
                            def open_review_row(e):
                                row=checklist_page['lookup'].get(e.args[1]['_id'])
                                if row is not None:show_issue(row)
                            table.on('rowClick', open_review_row)
                            page_numbers=ui.pagination(1,1,direction_links=True).props('max-pages=7 boundary-links :ellipses="false" :boundary-numbers="false"').classes('w-full justify-center').props('aria-label=확인목록페이지')
                            def show_review_page(pagination=None):
                                if checklist_page['syncing']:return
                                checklist_page['syncing']=True
                                try:
                                    rows,p=review_page(checklist_page['rows'],pagination or table.pagination)
                                    table.rows=review_table_rows(rows)
                                    table.pagination=p
                                    table.update()
                                    page_numbers.props['max']=max(1,(p['rowsNumber']+p['rowsPerPage']-1)//p['rowsPerPage'])
                                    page_numbers.set_value(p['page']);page_numbers.update()
                                finally:checklist_page['syncing']=False
                            def go_page(e):
                                show_review_page({**table.pagination,'page':e.value})
                            page_numbers.on_value_change(go_page)
                            table.on('request',lambda e:show_review_page(e.args['pagination']),['pagination'])
                            table.on_pagination_change(lambda e:show_review_page(e.value))
                        detail = ui.card().classes('detail-pane w-full').props('data-testid=issue-detail')
                        detail.set_visibility(False)
                    investigation = ui.column().classes('w-full')
            with ui.tab_panel(chat_tab):
                with ui.column().classes('chat-content') as chat_content:
                    with ui.row().classes('w-full items-center justify-between mb-4'):
                        chat_title=ui.label('검증 에이전트와 대화').classes('title')
                        with ui.row().classes('gap-1'):
                            ui.button('채팅 보관함',icon='folder_open',on_click=lambda:tabs.set_value(library_tab)).props('flat no-caps')
                            ui.button('결과 조회', on_click=lambda: launch('read')).props('flat no-caps')
                            ui.button('원인 조회', on_click=lambda: launch('investigate')).props('flat no-caps')
                    with ui.row().classes('w-full gap-2'):
                        selection_mode_button=ui.button('메시지 선택',icon='checklist',on_click=lambda:set_message_selection_mode(not message_selection_mode['active'])).props('flat dense')
                        select_all_messages=ui.button('전체 선택',icon='select_all',on_click=lambda:toggle_all_messages()).props('flat dense').tooltip('현재 대화의 모든 메시지 선택·해제')
                        delete_selected=ui.button('선택 삭제 (0)',icon='delete_outline',on_click=lambda:delete_messages(list(selected_messages))).props('flat dense color=negative')
                        select_all_messages.set_visibility(False)
                        delete_selected.set_visibility(False)
                    messages = ui.column().classes('w-full chat-messages').props('data-testid=chat-messages role=log aria-label=대화기록 aria-live=polite')
                    chat_status = ui.column().classes('w-full gap-2')
                    command_intake = ui.column().classes('w-full gap-2').props('data-testid=command-intake')
                    memory_preview = ui.column().classes('w-full gap-2').props('data-testid=memory-preview')
                    disposition_preview = ui.column().classes('w-full gap-2').props('data-testid=disposition-preview')
                    rule_preview = ui.column().classes('w-full gap-2').props('data-testid=rule-preview')
                    with ui.expansion('예외·비교 기준 관리',icon='bookmark_border').classes('w-full').props('data-testid=saved-memory-expansion') as memory_expansion:
                        ui.label('담당자가 저장한 예외 처리와 비교 기준입니다. 저장만 한 내용과 확인 후 적용한 내용을 구분합니다.').classes('muted')
                        if hasattr(adapter,'rate_catalog'):
                            ui.button('청구 항목 선택 · 단가 비교',icon='price_change',on_click=lambda:rate_dialog()).props('outline no-caps data-testid=rate-catalog-chat')
                        ui.label('채팅에서 정리한 예외·비교 기준을 보관합니다. 요청 저장 → 내용 확인 → 처리 또는 비교 결과 반영 순서로 진행합니다.').classes('muted mb-3')
                        ui.button('저장 목록 새로고침',on_click=lambda:load_saved_memory()).props('flat no-caps data-testid=refresh-saved-memory')
                        memory_records = ui.column().classes('w-full gap-3').props('data-testid=saved-memory-records')
                    memory_expansion.set_visibility(memory_supported)
                    memory_expansion.on_value_change(lambda e:load_saved_memory() if e.value else None)
                    questions = ui.column().classes('w-full')
            with ui.tab_panel(evidence_tab):
                ui.label('공개 체험은 포함된 가상 자료만 사용합니다.')
            with ui.tab_panel(monthly_tab):
                ui.label('공개 체험은 포함된 가상 자료만 사용합니다.')
                focus_month=lambda *args:None
            with ui.tab_panel(library_tab):
                ui.label('AI 대화는 사용하지 않습니다.')
    with ui.dialog().props('data-testid=dashboard-report-dialog') as report_dialog:
        with ui.card().classes('report-dialog'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('물류비 검증 요약').classes('title')
                ui.button(icon='close', on_click=report_dialog.close).props('flat round dense aria-label="보고서 닫기"')
            reports = ui.column().classes('w-full gap-3')
    cost_dialog = ui.dialog().props('data-testid=cost-section-dialog')
    with ui.column().classes('composer-dock gap-0') as dock:
        ui.button('새 메시지 보기', icon='arrow_downward').props('unelevated no-caps data-testid=latest-message').classes('latest-message').style('display:none')
        with ui.row().classes('chat-target-chip items-center justify-between flex-nowrap') as target_chip:
            target_chip.props('data-testid=chat-target-chip')
            target_chip_label = ui.label('').classes('text-xs break-words')
            clear_target_button = ui.button(icon='close', on_click=lambda: clear_chat_target()).props('flat round dense aria-label="대화 대상 해제"').tooltip('대화 대상 해제')
        target_chip.set_visibility(False)
        with ui.row().classes('composer w-full items-end flex-nowrap'):
            prompt = ui.textarea(placeholder='예: DEMO COMPANY 가상센터 2029년 7월 전체 청구 검증해줘').props('borderless autogrow rows=1').classes('grow')
            send = ui.button(icon='arrow_upward', on_click=lambda: submit()).props('round unelevated aria-label=메시지보내기').tooltip('메시지 보내기')
        ui.label('Enter로 전송 · Shift+Enter로 줄바꿈').classes('composer-note w-full')
        progress = ui.label('').classes('composer-note w-full')
    dock.set_visibility(False)

    def highlight_navigation(value):
        if value == '대화' or value == chat_tab:
            ui.run_javascript('setTimeout(() => {' + SCROLL_SCRIPT + '; window.skinChat?.open();}, 80)')
        if value == '채팅 보관함' or value == library_tab:value='대화'
        for target, button in nav_buttons.items():
            button.props('color=primary' if target == value else 'color=grey-7')
            button.style('background:#EAE1D5' if target == value else 'background:transparent')
    tabs.on_value_change(lambda e: highlight_navigation(e.value))
    highlight_navigation('대시보드')
    from navigation_history import bind_navigation_history
    bind_navigation_history(tabs, lambda: (report_dialog.close(), cost_dialog.close()))

    def open_chat(key):
        if not key:return
        adapter.manage_chat('select',chat_id=key)
        prompt.set_value('');tabs.set_value(chat_tab);refresh()
        ui.run_javascript('window.skinChat?.open()')

    def delete_messages(ids):
        if not ids:ui.notify('삭제할 메시지를 선택해 주세요.');return
        key=adapter.library.data['active_id']
        def commit():
            adapter.manage_chat('delete_messages',chat_id=key,message_ids=ids)
            set_message_selection_mode(False);refresh()
        confirm_delete(f'메시지 {len(ids)}개를 삭제할까요?',commit)

    def select_message(key, checked):
        if checked:selected_messages.add(key)
        else:selected_messages.discard(key)
        update_message_selection()

    def set_message_selection_mode(active):
        message_selection_mode['active']=active
        selection_mode_button.set_text('선택 취소' if active else '메시지 선택')
        chat_content.classes(add='selecting' if active else '',remove='' if active else 'selecting')
        select_all_messages.set_visibility(active)
        delete_selected.set_visibility(active)
        if not active:selected_messages.clear()
        for checkbox in message_checkboxes.values():
            checkbox.set_visibility(active)
            if not active:checkbox.set_value(False)
        update_message_selection()

    def update_message_selection():
        delete_selected.set_text(f'선택 삭제 ({len(selected_messages)})')
        keys={m['id'] for m,_ in rendered_messages}
        select_all_messages.set_text('전체 해제' if keys and keys.issubset(selected_messages) else '전체 선택')
        select_all_messages.set_enabled(bool(keys))

    def toggle_all_messages():
        if not message_selection_mode['active']:return
        keys={m['id'] for m,_ in rendered_messages}
        if keys and keys.issubset(selected_messages):selected_messages.clear()
        else:selected_messages.update(keys)
        for key,checkbox in message_checkboxes.items():checkbox.set_value(key in selected_messages)
        update_message_selection()

    def choose_chat():
        tabs.set_value(chat_tab)
        prompt.set_value(DEFAULT_REQUEST)

    def write_prompt(text):
        tabs.set_value(chat_tab)
        prompt.set_value(text)
        prompt.run_method('focus')
        ui.notify('입력창에 요청을 작성했습니다. 내용을 확인하고 Enter로 보내세요.')

    def start_verification():
        with ui.dialog() as dialog, ui.card().classes('w-96 max-w-full gap-4'):
            ui.label('검증할 월을 선택해 주세요').classes('section-title')
            ui.label('DEMO COMPANY · 가상센터').classes('muted')
            month=calendar_input('검증 월 (YYYY-MM)',month_only=True,placeholder='예: 2029-08').classes('w-full')
            ui.label('요청을 보내면 해당 월의 자료 등록 여부부터 확인합니다.').classes('muted')
            def prepare():
                value=(month.value or '').strip()
                if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',value):
                    ui.notify('검증 월을 2029-08 형식으로 입력해 주세요.',type='warning');return
                dialog.close()
                write_prompt(f'DEMO COMPANY 가상센터 {value} 전체 청구 검증해줘')
            with ui.row().classes('w-full justify-end'):
                ui.button('취소',on_click=dialog.close).props('flat')
                ui.button('요청 작성',on_click=prepare)
        dialog.open()

    def question_examples():
        with ui.dialog() as dialog, ui.card().classes('w-[480px] max-w-full gap-4'):
            ui.label('어떤 내용이 궁금하신가요?').classes('section-title')
            for text in ('현재 보고서에서 먼저 확인해야 할 문제는 무엇인가요?',
                         '주문 연결과 수량 일치는 어떤 차이가 있나요?',
                         '새 월을 검증하려면 어떤 자료가 필요한가요?'):
                def choose(value=text):
                    dialog.close();write_prompt(value)
                ui.button(text,on_click=choose).props('outline no-caps').classes('w-full')
            ui.button('닫기',on_click=dialog.close).props('flat')
        dialog.open()

    def launch(action, **kwargs):
        try:
            adapter.start(action, **kwargs)
            tabs.set_value(chat_tab)
            refresh()
        except Exception as exc:
            ui.notify(str(exc), type='negative')

    def chat_about_issue(row):
        ui.notify('AI 대화는 사용하지 않습니다. 상세의 비교 근거를 확인하세요.');return
        try:
            adapter.select_chat_targets([row['_id']], expected_result_version=row.get('_result_version'))
            tabs.set_value(chat_tab)
            prompt.set_value('')
            refresh()
            prompt.run_method('focus')
            ui.notify('대화할 청구 내역을 연결했습니다. 원하는 내용을 입력해 주세요.')
        except Exception as exc:
            ui.notify(str(exc), type='negative')

    async def rate_dialog():
        with ui.dialog() as dialog,ui.card().classes('w-[1100px] max-w-full gap-4'):
            ui.label('청구 항목 선택 · 단가 비교').classes('section-title')
            ui.label('정상 항목도 선택할 수 있습니다. 원본 수량과 금액은 유지하고 비교할 단가의 출처를 확인합니다.').classes('muted')
            content=ui.column().classes('w-full')
            with content:ui.spinner(size='24px')
            ui.button('닫기',on_click=dialog.close).props('flat no-caps')
        dialog.open()
        try:
            data=await run.io_bound(adapter.rate_catalog)
            content.clear()
            with content:
                ui.label(data['message']).classes('muted')
                rows=[]
                for row in data['rows']:
                    rows.append(dict(row,item=row['_rate_label'],source=row['청구위치'],
                        qty=format_number(row['_rate_quantity']),rate=format_number(row['_rate_price']),amount=format_number(row['청구값']),
                        applied=rule_labels.get(row.get('_rule_status'),''),new_difference=format_number(row.get('_rule_difference'))))
                columns=[('_invoice_row','청구 행'),('item','항목'),('_rate_unit','과금 단위'),('qty','기재 수량'),('rate','기재 단가'),('amount','청구금액'),('applied','새 기준 결과'),('new_difference','새 차이')]
                catalog=ui.table(columns=[{'name':k,'field':k,'label':v,'align':'right' if k in ('qty','rate','amount','new_difference') else 'left','sortable':True} for k,v in columns],
                    rows=rows,row_key='_id',selection='multiple',pagination=10).props('flat data-testid=rate-catalog-table').classes('w-full')
                search=ui.input('항목명·청구 행 검색').props('outlined dense clearable').classes('w-full')
                search.bind_value(catalog,'filter')
                def choose():
                    if not catalog.selected:
                        ui.notify('비교할 청구 항목을 선택해 주세요.',type='warning');return
                    try:
                        adapter.select_chat_targets([x['_id'] for x in catalog.selected],expected_target_version=data['version'])
                        dialog.close();tabs.set_value(chat_tab);prompt.set_value('');refresh();prompt.run_method('focus')
                        ui.notify('청구 항목을 연결했습니다. 적용할 단가 기준을 말씀해 주세요.')
                    except Exception as exc:ui.notify(str(exc),type='negative')
                ui.button('선택 항목으로 대화',on_click=choose).props('unelevated no-caps data-testid=rate-catalog-select')
        except Exception as exc:
            content.clear()
            with content:ui.label('청구 항목을 읽지 못했습니다. '+str(exc)).classes('text-red-600')

    def open_verification_result(item):
        if item['status']=='awaiting_user_decision' and item.get('conversation_id') in adapter.library.data['chats']:
            adapter.manage_chat('select',chat_id=item['conversation_id']);refresh()
        tabs.set_value(monthly_tab if item['destination']=='monthly' else chat_tab if item['status']=='awaiting_user_decision' else dashboard_tab)
        if item['destination']=='monthly' and callable(focus_month):focus_month(item['month'])

    def show_verification_notification(item):
        color={'completed':'positive','failed':'negative','awaiting_user_decision':'warning'}[item['status']]
        ui.notify(item['title'],type=color,position='top-right',timeout=6000,close_button='닫기')
        with ui.dialog() as dialog,ui.card().classes('w-[480px] max-w-full gap-4').props('data-testid=verification-notification'):
            ui.icon({'completed':'task_alt','failed':'error_outline','awaiting_user_decision':'help_outline'}[item['status']],color=color,size='32px')
            ui.label(item['title']).classes('section-title')
            ui.label(item['message']).classes('muted')
            with ui.row().classes('gap-3'):
                def view():dialog.close();open_verification_result(item)
                ui.button('질문 확인' if item['status']=='awaiting_user_decision' else '마지막 완료 결과 보기' if item['status']=='failed' else '결과 보기',on_click=view).props('unelevated no-caps data-testid=notification-view-result')
                ui.button('닫기',on_click=dialog.close).props('flat no-caps data-testid=notification-dismiss')
        dialog.open()

    def update_verification_notice(state):
        actual=state.get('verification_notification_job',state.get('verification_job',{}))
        item=verification_description(actual)
        signature=(actual.get('id'),actual.get('status'),actual.get('finished_at'))
        if signature!=banner_state['signature']:
            banner_state['signature']=signature;verification_banner.clear()
            verification_banner.set_visibility(item is not None)
            if item:
                compact = dashboard_variant == 'priority' and item['status'] == 'completed'
                with verification_banner,ui.row().classes('w-full items-center justify-between ' + ('priority-execution' if compact else 'p-4 rounded-xl')).style('background:transparent' if compact else 'background:#F0F3F2'):
                    with ui.column().classes('gap-1'):
                        ui.label('최근 실행 · '+item['title']).classes('muted' if compact else 'text-sm font-semibold')
                        if item.get('finished_at'):
                            stamp=datetime.fromtimestamp(item['finished_at'],timezone(timedelta(hours=9))).strftime('%m/%d %H:%M KST')
                            ui.label(stamp).classes('muted')
                        if not compact:ui.label(item['message']).classes('muted')
                    if item['status']=='running':ui.spinner(size='22px')
                    elif not compact:ui.button('질문 확인' if item['status']=='awaiting_user_decision' else '결과 보기',on_click=lambda data=item:open_verification_result(data)).props('outline no-caps data-testid=banner-view-result')
        notice=job_notifications.poll(actual)
        if notice:show_verification_notification(notice)

    def clear_chat_target():
        try:
            adapter.clear_chat_targets()
            refresh()
        except Exception as exc:
            ui.notify(str(exc), type='negative')

    def intake_choice(choice, value, draft_id):
        try:
            adapter.command_choice(choice, value=value, pending_id=draft_id)
            refresh()
        except Exception as exc:
            ui.notify(str(exc), type='negative')

    async def memory_action(action, **kwargs):
        if memory_ui['busy'] or rule_ui['busy'] or disposition_ui['busy']:
            return
        client = ui.context.client
        memory_ui.update(busy=True,message='저장할 내용을 확인하고 있습니다.' if action=='prepare' else '기록을 저장하고 있습니다.' if action=='confirm' else '저장 요청을 취소하고 있습니다.')
        refresh()
        try:
            method={'prepare':adapter.prepare_memory_preview,'confirm':adapter.confirm_memory_preview,'cancel':adapter.cancel_memory_preview}[action]
            await run.io_bound(method, **kwargs)
        except Exception as exc:
            with client:
                ui.notify(str(exc),type='negative')
        finally:
            memory_ui.update(busy=False,message='')
            refresh()
        if memory_expansion.value:
            await load_saved_memory()

    async def disposition_action(action, **kwargs):
        if disposition_ui['busy'] or memory_ui['busy'] or rule_ui['busy']:
            return
        client = ui.context.client
        disposition_ui.update(busy=True, message={
            'prepare': '담당자 처리 내용을 확인하고 있습니다.',
            'confirm': '담당자 처리 상태를 반영하고 있습니다.',
            'cancel': '담당자 처리 요청을 취소하고 있습니다.'}[action])
        refresh()
        try:
            method = {'prepare': adapter.prepare_disposition, 'confirm': adapter.confirm_disposition,
                      'cancel': adapter.cancel_disposition}[action]
            await run.io_bound(method, **kwargs)
        except Exception as exc:
            with client:
                ui.notify(str(exc), type='negative')
        finally:
            disposition_ui.update(busy=False, message='')
            refresh()
        if memory_expansion.value:
            await load_saved_memory()

    async def rule_action(operation, **kwargs):
        if rule_ui['busy'] or memory_ui['busy'] or disposition_ui['busy']:
            return
        client = ui.context.client
        rule_ui.update(busy=True, message={
            'prepare': '선택한 대상의 변경 전후 값을 계산하고 있습니다.',
            'lifecycle': '선택한 기준과 현재 자료로 변경 전후를 계산하고 있습니다.',
            'confirm': '확인한 비교 기준과 새 계산 결과를 저장하고 있습니다.',
            'cancel': '기준 적용 요청을 취소하고 있습니다.'}[operation])
        refresh()
        try:
            method = {'prepare': adapter.prepare_rule, 'confirm': adapter.confirm_rule,
                      'cancel': adapter.cancel_rule}.get(operation)
            if operation=='lifecycle':method=adapter.prepare_rule_lifecycle
            await run.io_bound(method, **kwargs)
        except Exception as exc:
            with client:
                ui.notify(str(exc), type='negative')
        finally:
            rule_ui.update(busy=False, message='')
            refresh()
        if memory_expansion.value:
            await load_saved_memory()

    async def configure_rule(record, container):
        client = ui.context.client
        container.clear()
        try:
            options = await run.io_bound(adapter.rule_options, record['record_id'])
            with container:
                if options.get('message'):
                    ui.label(str(options['message'])).classes('muted')
                choices = options.get('choices') or []
                if not options.get('supported') or not choices:
                    ui.label('이 요청에 적용할 계산 도구가 없습니다. 저장한 요청과 기존 결과는 유지합니다.').classes('text-sm')
                    return
                reapprove=bool(options.get('reapproval_required'))
                previous_spec=options.get('previous_spec')
                if reapprove and previous_spec:
                    selection=next((c for c in choices if c['kind']==previous_spec['kind']),None)
                    if not selection:
                        ui.label('이전 비교 방식은 현재 자료에서 지원되지 않습니다. 새 요청으로 대상을 확인해 주세요.').classes('muted')
                        return
                    ui.label('이전 적용 기준 · '+selection['label']).classes('text-sm font-semibold')
                    for param in selection.get('parameters',[]):
                        value=previous_spec['parameters'].get(param['name'])
                        if param['name']=='references':
                            from cost_references import labels
                            for text in labels(value or []):ui.label(text).classes('text-sm')
                        elif param.get('type')=='multi_select':
                            options=param.get('options') or {}
                            for i,key in enumerate(value or [],1):
                                ui.label(f"계산결과{i}: {options.get(key,'현재 사용할 수 없는 계산 기준')}").classes('text-sm')
                        else:
                            ui.label(param['label']+' · '+str((param.get('options') or {}).get(value,value))).classes('text-sm')
                    ui.button('현재 자료로 재계산',on_click=lambda:
                              rule_action('prepare',record_id=record['record_id'],kind=previous_spec['kind'],
                                          parameters=previous_spec['parameters'],reapprove=True)).props('outline no-caps data-testid=prepare-rule-reapproval')
                    return
                selection = ui.select({item['kind']:item['label'] for item in choices}, value=None,
                                      label='적용할 비교 방식').props('outlined dense data-testid=rule-kind').classes('w-full')
                fields_container = ui.column().classes('w-full gap-3')

                def render_parameters():
                    fields_container.clear()
                    selected = next((item for item in choices if item['kind'] == selection.value), None)
                    if selected is None:
                        return
                    with fields_container:
                        if selected.get('description'):
                            ui.label(str(selected['description'])).classes('text-sm')
                        fields = {}
                        for param in selected.get('parameters', []):
                            label = param['label'] + (' *' if param.get('required') else '')
                            field_type = param.get('type')
                            if field_type == 'select':
                                field = ui.select(param.get('options') or {}, value=param.get('value'), label=label)
                            elif field_type == 'multi_select':
                                field = ui.select(param.get('options') or {}, value=param.get('value') or [], label=label,
                                                  multiple=True, with_input=True).props('use-chips')
                            elif field_type == 'number':
                                field = ui.number(label, value=param.get('value'))
                            elif field_type == 'text':
                                field = ui.input(label, value=param.get('value') or '')
                            else:
                                ui.label('지원하지 않는 입력 형식입니다. 이 비교 방식은 적용할 수 없습니다.').classes('text-sm text-red-600')
                                return
                            field.props(f'outlined dense data-testid=rule-param-{param["name"]}').classes('w-full')
                            fields[param['name']] = field
                        if 'references' in fields:
                            reference_hint=ui.column().classes('w-full gap-1').props('data-testid=cost-reference-mapping')
                            def show_references():
                                from cost_references import labels
                                reference_hint.clear()
                                with reference_hint:
                                    for text in labels(fields['references'].value or []):ui.label(text).classes('text-sm')
                                    if fields['references'].value:ui.label('참조합계 = 선택한 청구 금액의 합계').classes('muted')
                            fields['references'].on_value_change(show_references)
                            show_references()
                        if 'result_references' in fields:
                            result_hint=ui.column().classes('w-full gap-1').props('data-testid=result-reference-mapping')
                            result_options=next(p.get('options',{}) for p in selected['parameters'] if p['name']=='result_references')
                            def show_results():
                                result_hint.clear()
                                with result_hint:
                                    for i,key in enumerate(fields['result_references'].value or [],1):
                                        ui.label(f"계산결과{i}: {result_options.get(key,'현재 사용할 수 없는 계산 기준')}").classes('text-sm')
                                    if fields['result_references'].value:
                                        ui.label('계산결과합계 = 선택한 기준을 원본에서 다시 계산한 금액의 합계').classes('muted')
                            fields['result_references'].on_value_change(show_results)
                            show_results()
                        for limitation in selected.get('limitations') or []:
                            ui.label(str(limitation)).classes('muted')

                        async def calculate():
                            missing = [param['label'] for param in selected.get('parameters', [])
                                       if param.get('required') and fields[param['name']].value in (None, '')]
                            if missing:
                                ui.notify('입력해 주세요: ' + ', '.join(missing), type='warning')
                                return
                            await rule_action('prepare', record_id=record['record_id'], kind=selected['kind'],
                                              parameters={name:field.value for name, field in fields.items()},**({'reapprove':True} if reapprove else {}))
                        ui.button('변경 전후 계산', on_click=calculate).props('outline no-caps data-testid=prepare-rule')
                selection.on_value_change(render_parameters)
                ui.label('선택한 청구 항목만 계산합니다. 적용 전 변경 내용을 다시 확인합니다.').classes('muted')
        except Exception as exc:
            with client:
                ui.notify(str(exc), type='negative')

    async def show_rule_history(record,container):
        client=ui.context.client
        container.clear()
        try:
            events=await run.io_bound(adapter.rule_history,record['record_id'])
            with container:
                choices={}
                for event in events:
                    if event.get('stopped'):continue
                    spec=event['payload']['spec'];params=spec['parameters']
                    text=({'include':'소모품 포함','exclude':'소모품 제외'}.get(params.get('consumables')) or
                          {'claim':'청구 기재 단가','contract':'계약 기재 단가'}.get(params.get('rate_source')) or
                          f"지정 단가 {params.get('rate')}원/{params.get('unit')}")
                    if spec['kind']=='quantity_rematch':
                        from quantity_rematch import JOINS
                        text=f"{params['start']}~{params['end']} · {JOINS[params['join']]}"
                    elif spec['kind']=='b2c_link':
                        from b2c_link_rules import JOINS
                        text=f"B2C {params['start']}~{params['end']} · {JOINS[params['join']]}"
                    elif spec['kind']=='pack_conversion':
                        from pack_conversion_rules import description
                        text=description(params)
                    elif spec['kind']=='formula':
                        text='조건: '+params['condition']+' / 계산식: '+params['expression']
                    choices[event['rule_id']]=f"버전 {event['rule_version']} · {text} · {len(event['payload']['targets'])}개 항목"
                if not choices:
                    ui.label('비교하거나 복원할 적용 버전이 없습니다.').classes('muted');return
                selection=ui.select(choices,value=None,label='이전 적용 기준 선택').props('outlined dense data-testid=rule-history-choice').classes('w-full')
                ui.label('먼저 현재 자료로 비교할 수 있습니다. 복원은 재계산 결과를 확인한 뒤 새 버전으로 저장합니다.').classes('muted')
                async def start(action):
                    if not selection.value:
                        ui.notify('이전 적용 기준을 선택해 주세요.',type='warning');return
                    await rule_action('lifecycle',record_id=record['record_id'],action=action,selected_rule_id=selection.value)
                with ui.row().classes('gap-2'):
                    ui.button('현재 자료로 비교',on_click=lambda:start('compare')).props('outline no-caps data-testid=compare-old-rule')
                    ui.button('이 기준으로 복원 검토',on_click=lambda:start('restore')).props('outline no-caps data-testid=prepare-restore-rule')
                    ui.button('보관 자료로 재현 확인',on_click=lambda:start('exact_replay')).props('flat no-caps data-testid=exact-replay-rule')
                with ui.expansion('적용·중지 기록').classes('w-full'):
                    for event in events:
                        label='중지' if event.get('stopped') else '복원 적용' if event['payload'].get('lifecycle',{}).get('action')=='restore' else '기준 적용'
                        ui.label(f"버전 {event['rule_version']} · {label} · {event['payload']['month']}").classes('text-sm')
        except Exception as exc:
            with client:ui.notify(str(exc),type='negative')

    def render_rule_preview(state, running):
        rule_preview.clear()
        if not rule_supported:
            return
        preview = state.get('rule_preview') or {}
        with rule_preview:
            if rule_ui['busy']:
                with ui.row().classes('items-center gap-2'):
                    ui.spinner(size='18px')
                    ui.label(rule_ui['message']).classes('muted')
            if not preview:
                return
            status = preview.get('state') or preview.get('status')
            with ui.card().classes('command-card w-full').props(f'data-testid=rule-preview-card data-status={status}'):
                action=preview.get('lifecycle_action')
                recurring=(preview.get('spec') or {}).get('kind') in ('recurring_rate','recurring_quantity','formula') and preview.get('scope')=='future'
                recurring_quantity=(preview.get('spec') or {}).get('kind')=='recurring_quantity'
                title=({'stop':'비교 기준 중지','restore':'이전 기준 복원','compare':'현재 자료와 과거 기준 비교','exact_replay':'당시 자료·코드 재현 확인'}[action]+(' 완료' if status=='applied' else ' · 결과 확인')) if action else (('현재 자료의 기준 재승인 완료' if status=='applied' else '변경 자료 재승인 · 대상 확인') if preview.get('reapproval_required') else '새 비교 기준 적용 완료' if status == 'applied' else '비교 기준 변경 전후 확인')
                if recurring and not action:
                    title='계속 적용할 단가 등록 완료' if status=='applied' else '계속 적용할 단가 · 조건 확인'
                    if (preview.get('spec') or {}).get('kind')=='formula':title='조건·수식 등록 완료' if status=='applied' else '조건·수식 · 적용 범위 확인'
                    elif recurring_quantity:title='계속 적용할 수량 기준 등록 완료' if status=='applied' else '계속 적용할 수량 기준 · 범위 확인'
                ui.label(title).classes('section-title')
                memory_summary(preview, targets=False)
                if action and preview.get('transitions'):
                    ui.label('이전 적용 기록과 이번 재계산의 차이').classes('font-semibold text-sm')
                    cols=[('label','대상'),('previous_state','이전 기록 상태'),('previous_comparison','이전 비교값'),('previous_difference','이전 차이'),('new_comparison','이번 비교값'),('new_difference','이번 차이'),('unit','단위')]
                    ui.table(columns=[{'name':k,'field':k,'label':label,'align':'left' if k in ('label','previous_state','unit') else 'right'} for k,label in cols],rows=[dict(row,id=i) for i,row in enumerate(preview['transitions'])],row_key='id',pagination=5).classes('w-full').props('flat wrap-cells data-testid=rule-lifecycle-table')
                if preview.get('reapproval_required') and not action:
                    ui.label('이전 기록은 보존하고, 표시된 조건과 적용 범위로 새 버전을 저장합니다.').classes('muted')
                    columns=[('label','대상'),('previous_locator','이전 위치'),('current_locator','현재 위치'),('previous_claim','이전 청구값'),('current_claim','현재 청구값'),('previous_comparison','이전 비교값'),('current_comparison','현재 비교값')]
                    ui.table(columns=[{'name':k,'field':k,'label':label,'align':'left'} for k,label in columns],rows=preview.get('rebound_targets',[]),row_key='label',pagination=5).classes('w-full').props('flat wrap-cells data-testid=reapproval-target-table')
                    with ui.expansion(f"변경된 자료 {len(preview.get('changed_files',[]))}개 보기").classes('w-full'):
                        for item in preview.get('changed_files',[]):
                            ui.label(item['change']+' · '+re.split(r'[/\\]',item['path'])[-1]).classes('text-sm').tooltip(item['path'])
                ui.label('기존 계산 결과는 보존하며, 선택한 항목의 새 기준 결과를 별도로 기록합니다. 전체 청구 재검증이나 금액 승인을 뜻하지 않습니다.').classes('muted')
                if preview.get('message'):
                    ui.label(str(preview['message'])).classes('text-sm break-words')
                changes = preview.get('changes') or []
                if (preview.get('spec') or {}).get('kind')=='b2c_link':
                    from b2c_link_rules import JOINS
                    p=preview['spec']['parameters']
                    ui.label(f"WMS 출고일 {p['start']} ~ {p['end']} · {JOINS[p['join']]}").classes('font-semibold text-sm')
                if (preview.get('spec') or {}).get('kind')=='quantity_rematch':
                    from quantity_rematch import JOINS
                    p=preview['spec']['parameters']
                    ui.label(f"{p['start']} ~ {p['end']} · {JOINS[p['join']]}").classes('font-semibold text-sm')
                    if p['filter_field']!='none':
                        ui.label(f"청구 {'채널명' if p['filter_field']=='counterparty' else '품번'}: {p['filter_value']} · {'해당 대상만 비교' if p['filter_mode']=='include' else '해당 대상 비교 제외'}").classes('text-sm')
                if (preview.get('spec') or {}).get('kind')=='pack_conversion':
                    from pack_conversion_rules import description
                    ui.label(description(preview['spec']['parameters'])).classes('font-semibold text-sm')
                if (preview.get('spec') or {}).get('kind')=='formula':
                    p=preview['spec']['parameters']
                    ui.label('조건: '+p['condition']+' / 계산식: '+p['expression']).classes('font-semibold text-sm')
                    from cost_references import labels
                    for text in labels(p.get('references',[])):ui.label(text).classes('text-sm')
                if changes:
                    rule_change_table(changes)
                for limitation in preview.get('limitations') or []:
                    ui.label(str(limitation)).classes('text-sm').style('color:#a86a08')
                if recurring:
                    p=preview['spec']['parameters']
                    if preview['spec']['kind']=='formula':
                        ui.label(p['start_month']+'부터 · 조건: '+p['condition']+' / 계산식: '+p['expression']).classes('font-semibold text-sm')
                    elif recurring_quantity:
                        from recurring_quantity_rules import description
                        ui.label(description(p)).classes('font-semibold text-sm')
                    else:
                        ui.label(f"{p['start_month']}부터 · {p['category']} / {p['item']} / {p['unit']} · 단가 {p['rate']}원").classes('font-semibold text-sm')
                    ui.label('앞으로만 적용: 위 계산은 현재 수량으로 본 예시이며 이번 청구 결과는 변경하지 않습니다.' if preview.get('application')=='future_only' else '이번 선택 건과 시작월 이후 확인한 범위에 적용합니다.').classes('muted')
                    ui.label('해당 월 검증 때 자동 비교합니다. 이미 완료한 월은 재검증해야 반영됩니다.').classes('muted')
                elif preview.get('application') in ('future_only', 'current_and_future') or preview.get('scope') == 'future':
                    ui.label('앞으로의 청구에 자동 적용하지 않습니다. 이번에 계산한 선택 대상만 확인합니다.').classes('muted')
                if status in ('preview', 'awaiting_confirmation'):
                    with ui.row().classes('gap-2'):
                        ui.button('확인하고 기준 중지' if action=='stop' else '확인하고 이전 기준 복원' if action=='restore' else '현재 자료에 재승인·적용' if preview.get('reapproval_required') else '확인하고 기준 적용', on_click=lambda p=preview:
                                  rule_action('confirm', preview_id=p['preview_id'], preview_hash=p['preview_hash'], nonce=p['nonce'])).props('unelevated no-caps data-testid=confirm-rule').set_enabled(not running)
                        ui.button('기준 적용 취소', on_click=lambda p=preview:
                                  rule_action('cancel', preview_id=p['preview_id'])).props('flat no-caps data-testid=cancel-rule').set_enabled(not running)
                elif status == 'applied' and preview.get('application')!='future_only':
                    if any(c.get('check') == '선택 단가 금액 비교' for c in preview.get('changes', [])):
                        ui.button('새 기준 결과 조회', on_click=rate_dialog).props('outline no-caps data-testid=view-rule-items')
                    else:
                        ui.button('새 기준 결과 조회', on_click=lambda: tabs.set_value(results_tab)).props('outline no-caps data-testid=view-rule-items')

    def rule_change_table(changes):
        rows = []
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                rows.append({'id':index,'label':str(change)})
                continue
            before, after = change.get('before') or {}, change.get('after') or {}
            rows.append({'id':index, **change,
                         'label':change.get('label') or change.get('check') or '선택 항목',
                         'before_comparison':format_number(before.get('comparison')),
                         'after_comparison':format_number(after.get('comparison')),
                         'before_difference':format_number(before.get('difference')),
                         'after_difference':format_number(after.get('difference')),
                         'before_status':rule_labels.get(before.get('status'), before.get('status') or '—'),
                         'after_status':rule_labels.get(after.get('status'), after.get('status') or '—'),
                         'unit':after.get('unit') or before.get('unit') or '—'})
        columns = [
            ('label','대상'), ('before_comparison','기존 비교값'), ('after_comparison','새 비교값'),
            ('before_difference','기존 차이'), ('after_difference','새 차이'),
            ('before_status','기존 판정'), ('after_status','새 기준 판정'), ('unit','단위')]
        if changes and all(c.get('check')=='B2C 주문 연결' for c in changes):
            columns=[('label','청구 대상'),('before_status','기존 연결'),('after_status','새 기준 연결')]
        ui.table(columns=[{'name':key,'field':key,'label':label,'align':'left' if key in ('label','before_status','after_status') else 'right'}
                          for key,label in columns], rows=rows, row_key='id', pagination=10).props('flat wrap-cells data-testid=rule-change-table').classes('w-full')
        with ui.expansion('계산 근거', icon='receipt_long').classes('w-full'):
            for change in rows:
                if change.get('basis') or change.get('evidence_refs'):
                    ui.label(str(change.get('label') or '선택 항목')).classes('text-sm font-semibold')
                    if change.get('basis'):
                        ui.label(str(change['basis'])).classes('text-sm break-words')
                    for ref in change.get('evidence_refs') or []:
                        ui.label(str(ref)).classes('muted break-all')

    def memory_scope_text(record):
        application=record.get('application')
        return {'current_case':'이번 청구만','future_only':'앞으로의 청구만','current_and_future':'이번 청구와 앞으로 모두'}.get(application,'이번 청구만' if record.get('scope')=='once' else '범위 확인 필요')

    def memory_summary(record, *, targets=True):
        month=record.get('month') or record.get('invoice_month') or '청구월 확인 필요'
        warehouse=record.get('warehouse') or record.get('warehouse_id') or '가상센터'
        items=record.get('targets') or []
        count=record.get('target_count',len(items))
        ui.label(f'{month} · {warehouse} · 확인 항목 {count:,}개').classes('text-sm font-semibold')
        if record.get('summary'):
            ui.label(str(record['summary'])).classes('text-sm break-words')
        ui.label('저장 범위 · '+memory_scope_text(record)).classes('text-sm')
        if record.get('reason'):
            ui.label('처리 사유 · '+str(record['reason'])).classes('text-sm break-words')
        criterion=record.get('criterion')
        if isinstance(criterion,dict):criterion=criterion.get('predicate')
        if criterion:
            ui.label('요청한 조건 · '+str(criterion)).classes('text-sm break-words')
        if record.get('effective_from_month'):
            ui.label('시작 청구월 · '+str(record['effective_from_month'])).classes('text-sm')
        if targets and items:
            with ui.expansion(f'대상 {len(items):,}건과 원본 위치',icon='receipt_long').classes('w-full'):
                for item in items:
                    ui.label(item.get('label') or item.get('check') or '선택한 청구건').classes('text-sm font-medium break-words')
                    location=' · '.join(v for v in (re.split(r'[/\\]',str(item.get('source') or ''))[-1],str(item.get('locator') or '')) if v)
                    if location:ui.label(location).classes('muted break-all')

    saved_memory_view = {'request': 0, 'signature': None}

    async def load_saved_memory():
        if not memory_supported:
            return
        saved_memory_view['request'] += 1
        request_id = saved_memory_view['request']
        try:
            result=await run.io_bound(adapter.saved_memory)
            records=result.get('records',[]) if isinstance(result,dict) else result
            if request_id != saved_memory_view['request']:
                return
            signature = repr(records)
            if signature == saved_memory_view['signature']:
                return  # Preserve an open editor when overlapping refreshes return the same records.
            saved_memory_view['signature'] = signature
            memory_records.clear()
            with memory_records:
                if not records:
                    ui.label('저장된 예외 처리나 비교 기준이 없습니다.').classes('muted')
                for record in records:
                    with ui.card().classes('w-full gap-2 p-4').props('data-testid=saved-memory-record'):
                        latest = record.get('latest_disposition') or {}
                        latest_rule = record.get('latest_rule') or {}
                        review_status = record.get('review_status') or latest.get('effective_status') or latest.get('disposition')
                        if latest.get('invalidated'):
                            review_status = 'review_required'
                        ui.label('비교 기준 중지됨 · 원래 계산 유지' if latest_rule.get('stopped') else '새 기준 적용 · 자료 변경으로 재검토 필요' if latest_rule.get('invalidated') else
                                 '선택 항목의 새 기준 계산 저장됨' if latest_rule else
                                 '이전 처리 기록 · 새 확인으로 대체됨' if latest.get('superseded') else
                                 '담당자 처리 · ' + review_labels.get(review_status, str(review_status)) if review_status else
                                 '기록 저장됨 · 검증 결과에는 미적용').classes('text-sm font-semibold')
                        memory_summary(record)
                        if latest.get('invalidated') and not latest.get('superseded'):
                            ui.label('자료·비교 근거가 바뀌어 재검토가 필요합니다. 현재 확인 목록에서 대상을 다시 선택해 주세요.').classes('text-sm').style('color:#a86a08')
                        if record.get('created_at') or record.get('saved_at'):
                            ui.label('저장 시각 · '+str(record.get('created_at') or record['saved_at'])).classes('muted')
                        if disposition_supported and not latest.get('invalidated') and record.get('intent') == 'set_disposition' and record.get('application') in ('current_case', 'current_and_future'):
                            ui.label('검토한 내역을 표시하는 선택 기능입니다. 처리완료는 대시보드 정상에 포함하며 원래 계산값과 차이는 보존합니다.').classes('muted')
                            with ui.row().classes('items-center gap-3 w-full'):
                                choice = ui.select({key: review_labels[key] for key in ('reviewed', 'exception_accepted')},
                                                   value=None, label='처리 방식 선택').props('outlined dense data-testid=disposition-choice').classes('w-52')
                                ui.button('처리 내용 확인', on_click=lambda r=record, c=choice:
                                          disposition_action('prepare', record_id=r['record_id'], status=c.value)).props('outline no-caps data-testid=prepare-disposition')
                            if record.get('application') == 'current_and_future':
                                ui.label('이번에는 현재 청구의 선택 대상만 처리합니다. 앞으로의 자동 적용은 아직 연결되지 않았습니다.').classes('muted')
                        elif rule_supported and record.get('intent') == 'upsert_rule':
                            rule_config = ui.column().classes('w-full gap-3').props('data-testid=rule-configuration')
                            ui.button('비교 기준 설정', on_click=lambda r=record, c=rule_config:
                                      configure_rule(r, c)).props('outline no-caps data-testid=configure-rule')
                            if latest_rule:
                                spec=latest_rule['payload']['spec']
                                if spec['kind']=='formula':
                                    p=spec['parameters']
                                    ui.label('조건: '+p['condition']+' / 계산식: '+p['expression']).classes('muted')
                                elif spec['kind'] in ('recurring_rate','recurring_quantity'):
                                    p=spec['parameters']
                                    state_label='중지됨' if latest_rule.get('stopped') else '재확인 필요' if latest_rule.get('invalidated') else '다음 월 검증에 적용'
                                    if spec['kind']=='recurring_quantity':
                                        from recurring_quantity_rules import description
                                        ui.label(state_label+' · '+description(p)).classes('muted')
                                    else:
                                        ui.label(f"{state_label} · {p['start_month']}부터 · {p['category']} / {p['item']} / {p['unit']} · {p['rate']}원").classes('muted')
                                else:
                                    ui.label('원래 결과와 새 기준 결과는 구분해 보관합니다. 다른 청구의 자동 적용은 실행하지 않았습니다.').classes('muted')
                                if hasattr(adapter,'prepare_rule_lifecycle'):
                                    with ui.row().classes('gap-2'):
                                        if not latest_rule.get('stopped'):
                                            ui.button('기준 중지 검토',on_click=lambda r=record:rule_action('lifecycle',record_id=r['record_id'],action='stop')).props('outline no-caps data-testid=prepare-stop-rule')
                                        ui.button('이전 기준 비교·복원',on_click=lambda r=record,c=rule_config:show_rule_history(r,c)).props('flat no-caps data-testid=rule-history')
                        else:
                            ui.label('이 요청의 계산 적용 또는 미래 청구 자동 처리는 아직 지원하지 않습니다.').classes('muted')
        except Exception as exc:
            if request_id != saved_memory_view['request']:
                return
            saved_memory_view['signature'] = None
            memory_records.clear()
            with memory_records:
                ui.label('저장 목록을 읽지 못했습니다. '+str(exc)).classes('text-sm text-red-600')

    async def open_rule_management():
        memory_expansion.set_value(True)
        await load_saved_memory()
        ui.run_javascript(f'document.getElementById("c{memory_expansion.id}")?.scrollIntoView({{behavior:"smooth",block:"start"}})')

    def render_disposition_preview(state, running):
        disposition_preview.clear()
        if not disposition_supported:
            return
        preview = state.get('disposition_preview') or {}
        with disposition_preview:
            if disposition_ui['busy']:
                with ui.row().classes('items-center gap-2'):
                    ui.spinner(size='18px')
                    ui.label(disposition_ui['message']).classes('muted')
            if not preview:
                return
            status = preview.get('state') or preview.get('status')
            with ui.card().classes('command-card w-full').props(f'data-testid=disposition-preview-card data-status={status}'):
                ui.label('담당자 처리 반영 완료' if status == 'applied' else '담당자 처리 내용 확인').classes('section-title')
                memory_summary(preview)
                requested = preview.get('requested_status') or preview.get('disposition')
                ui.label('처리 방식 · ' + review_labels.get(requested, requested or '선택 필요')).classes('text-sm font-semibold')
                before = preview.get('before') or preview.get('before_statuses')
                if isinstance(before, dict) and before:
                    ui.label('반영 전 · ' + ' / '.join(f'{review_labels.get(key,key)} {value}개' for key, value in before.items())).classes('muted')
                elif preview.get('before_status'):
                    ui.label('반영 전 · ' + review_labels.get(preview['before_status'], preview['before_status'])).classes('muted')
                ui.label('계산 결과와 담당자 처리는 별개입니다. 수량·금액·차이 및 정상/불일치/미검증 판정은 유지합니다.').classes('muted')
                if preview.get('scope') == 'future':
                    ui.label('현재 청구의 선택 대상에만 처리 상태를 반영합니다. 앞으로의 자동 적용은 아직 실행하지 않습니다.').classes('muted')
                if preview.get('replaces_invalidated_ids'):
                    ui.label(f"이전 자료의 재검토 기록 {len(preview['replaces_invalidated_ids']):,}개를 이번 확인으로 대체합니다. 이전 기록은 보관합니다.").classes('text-sm')
                if preview.get('message'):
                    ui.label(str(preview['message'])).classes('text-sm break-words')
                if status in ('preview', 'awaiting_confirmation'):
                    with ui.row().classes('gap-2'):
                        ui.button('확인하고 처리 반영', on_click=lambda p=preview:
                                  disposition_action('confirm', preview_id=p['preview_id'], preview_hash=p['preview_hash'], nonce=p['nonce'])).props('unelevated no-caps data-testid=confirm-disposition').set_enabled(not running)
                        ui.button('처리 취소', on_click=lambda p=preview:
                                  disposition_action('cancel', preview_id=p['preview_id'])).props('flat no-caps data-testid=cancel-disposition').set_enabled(not running)
                elif status == 'applied':
                    ui.button('처리한 항목 조회', on_click=lambda: show_reviewed()).props('outline no-caps data-testid=view-disposition-items')
                elif status not in ('cancelled',) and preview.get('preview_id'):
                    ui.button('처리 안내 닫기', on_click=lambda p=preview:
                              disposition_action('cancel', preview_id=p['preview_id'])).props('flat no-caps').set_enabled(not running)

    def render_memory_preview(state, running):
        memory_preview.clear()
        if not memory_supported:
            return
        preview=state.get('memory_preview') or {}
        with memory_preview:
            if memory_ui['busy']:
                with ui.row().classes('items-center gap-2'):
                    ui.spinner(size='18px')
                    ui.label(memory_ui['message']).classes('muted')
            if not preview:
                return
            status=preview.get('state') or preview.get('status')
            with ui.card().classes('command-card w-full').props('data-testid=memory-preview-card') as card:
                card.props(f'data-status={status}')
                saved=status in ('saved','saved_pending_application')
                ui.label('기록 저장 완료' if saved else '저장할 내용 확인' if status in ('preview','awaiting_confirmation') else '저장 요청 확인').classes('section-title')
                memory_summary(preview)
                if saved:
                    applied = state.get('disposition_preview') or {}
                    handler_applied = applied.get('state') == 'applied' and applied.get('record_id') == preview.get('record_id')
                    rule_applied = state.get('rule_preview') or {}
                    comparison_applied = rule_applied.get('state') == 'applied' and rule_applied.get('record_id') == preview.get('record_id')
                    ui.label('선택한 항목의 새 기준 계산을 별도로 저장했습니다.' if comparison_applied else
                             '담당자 처리 상태를 반영했습니다. 계산 결과는 그대로입니다.' if handler_applied else
                             '검증 결과에는 아직 적용하지 않았습니다.').classes('text-sm font-semibold').style('color:#745319')
                    ui.label('채팅을 삭제해도 이 기록은 보관됩니다. 아래 저장 목록에서 다시 확인할 수 있습니다.').classes('muted')
                else:
                    ui.label('원래 수량·금액·판정과 보고서는 유지합니다. 이번 단계에서는 확인한 요청만 저장합니다.').classes('muted')
                if preview.get('scope')=='future':
                    ui.label('앞으로의 적용 조건을 기록합니다. 새 청구에 자동 적용하는 기능은 아직 연결되지 않았습니다.').classes('muted')
                if preview.get('message'):
                    ui.label(preview['message']).classes('text-sm break-words')
                conflicts=preview.get('conflicts') or []
                if conflicts:
                    ui.label(f'범위가 겹치는 기존 기록 {len(conflicts):,}개가 있습니다. 기존 기록을 덮어쓰지 않습니다.').classes('text-sm text-red-600')
                if status in ('preview','awaiting_confirmation') and not conflicts:
                    with ui.row().classes('gap-2'):
                        ui.button('확인하고 기록 저장',on_click=lambda p=preview:memory_action('confirm',preview_id=p['preview_id'],preview_hash=p['preview_hash'],nonce=p['nonce'])).props('unelevated no-caps data-testid=confirm-memory-save').set_enabled(not running)
                        ui.button('저장 취소',on_click=lambda p=preview:memory_action('cancel',preview_id=p['preview_id'])).props('flat no-caps data-testid=cancel-memory-save').set_enabled(not running)
                elif not saved and status not in ('cancelled',) and preview.get('preview_id'):
                    ui.button('저장 요청 닫기',on_click=lambda p=preview:memory_action('cancel',preview_id=p['preview_id'])).props('flat no-caps').set_enabled(not running)

    def render_command_intake(state, running):
        """Show the server-resolved draft; a choice is never a saved business rule."""
        selection = state.get('chat_selection') or {}
        target_chip.set_visibility(bool(selection))
        target_chip_label.set_text(selection.get('label') or '선택한 청구 내역으로 대화 중' if selection else '')
        clear_target_button.set_enabled(not running)
        command_intake.clear()
        pending = state.get('command_intake') or {}
        if not pending:
            return
        status = pending.get('state', pending.get('status', ''))
        stored_preview_state=(state.get('memory_preview') or {}).get('state')
        if status in ('draft_ready','saved_pending_application') and stored_preview_state in ('preview','awaiting_confirmation','saved','saved_pending_application'):
            return
        draft_id = pending.get('draft_id', pending.get('pending_id'))
        detail_field = next((q.get('field') for q in pending.get('questions') or [] if q.get('field') in ('reason','criterion')),None)
        labels = {'awaiting_target':'어느 청구건인가요?', 'awaiting_scope':'어디까지 적용할까요?',
                  'awaiting_application':'현재 청구에도 적용할까요?',
                  'awaiting_criteria':'앞으로 적용할 조건을 알려주세요',
                  'awaiting_details':'앞으로 적용할 조건을 알려주세요',
                  'draft_ready':'요청 내용 확인', 'unsupported':'지원 범위 안내',
                  'saved_pending_application':'기록 저장 완료',
                  'cancelled':'요청 정리를 취소했습니다', 'invalidated':'청구 내역을 다시 선택해 주세요',
                  'version_conflict':'청구 내역을 다시 선택해 주세요', 'read_only':'선택한 내역 안내'}
        with command_intake:
            with ui.card().classes('command-card w-full').props('data-testid=command-intake-card') as card:
                card.props(f'data-status={status} data-draft-id={draft_id}')
                title = labels.get(status,'요청 확인')
                if status in ('awaiting_criteria','awaiting_details'):
                    title = '처리 사유를 알려주세요' if detail_field == 'reason' else ('앞으로 적용할 조건을 알려주세요' if pending.get('scope')=='future' else '비교 기준을 알려주세요')
                ui.label(title).classes('section-title')
                # The intake message is also retained as the assistant reply.
                # Show each prompt once without removing history or choice data.
                prompt_key = lambda text: ' '.join(display_explanation(str(text or '')).split())
                shown_prompts = {prompt_key(title)}
                latest_message = (state.get('messages') or [{}])[-1]
                if latest_message.get('role') == 'assistant':
                    shown_prompts.add(prompt_key(latest_message.get('text')))

                def prompt_once(text, classes='text-sm leading-6 break-words'):
                    key = prompt_key(text)
                    if key and key not in shown_prompts:
                        shown_prompts.add(key)
                        ui.label(display_explanation(str(text))).classes(classes)

                prompt_once(pending.get('summary'))
                targets = pending.get('targets') or []
                if targets:
                    def target_rows():
                        for target in targets:
                            with ui.column().classes('command-target gap-1'):
                                ui.label(target.get('label') or '선택한 청구 내역').classes('text-sm font-semibold break-words')
                                if target.get('check'):
                                    ui.label(target['check']).classes('muted')
                                source_name = re.split(r'[/\\]',str(target.get('source') or ''))[-1]
                                location = ' · '.join(x for x in (source_name,str(target.get('locator') or '')) if x)
                                if location:
                                    ui.label(location).classes('muted break-all')
                    if len(targets) == 1:
                        target_rows()
                    else:
                        with ui.expansion(f'선택한 대상 {len(targets):,}개 확인',icon='receipt_long').classes('w-full'):
                            target_rows()
                choices = []
                for question in pending.get('questions') or []:
                    prompt_once(question.get('question') or question.get('label'), 'text-sm font-medium')
                    for option in question.get('options') or []:
                        choices.append({'choice':question.get('field'), 'value':option.get('value'), 'label':option.get('label','선택')})
                # The service supplies explicit choice names when a field needs a different action.
                choices = pending.get('options') or choices
                if choices:
                    with ui.row().classes('gap-2 w-full'):
                        for option in choices:
                            candidate = next((item for item in pending.get('candidates') or [] if item.get('target_id') == option.get('value')),None) if option.get('choice')=='select_target' else None
                            with ui.column().classes('gap-1 min-w-0'):
                                button = ui.button(option['label'], on_click=lambda item=option, did=draft_id: intake_choice(item['choice'],item.get('value'),did)).props('outline no-caps')
                                button.set_enabled(not running)
                                if candidate:
                                    source_name = re.split(r'[/\\]',str(candidate.get('source') or ''))[-1]
                                    location = ' · '.join(value for value in (source_name,str(candidate.get('locator') or '')) if value)
                                    if location:
                                        ui.label(location).classes('muted break-all')
                if status in ('awaiting_criteria','awaiting_details') and detail_field == 'reason':
                    reason_input = ui.textarea('담당자 처리 사유',value=pending.get('reason') or '').props('outlined autogrow').classes('w-full')
                    def reason_choice():
                        value = (reason_input.value or '').strip()
                        if not value:
                            ui.notify('처리 사유를 입력해 주세요.',type='warning')
                            return
                        intake_choice('reason',value,draft_id)
                    reason_button = ui.button('처리 사유 확인',on_click=reason_choice).props('unelevated no-caps')
                    reason_input.set_enabled(not running)
                    reason_button.set_enabled(not running)
                elif status in ('awaiting_criteria','awaiting_details'):
                    future_scope = pending.get('scope') == 'future'
                    criterion = pending.get('criterion') or {}
                    if not isinstance(criterion,dict):
                        criterion = {'predicate':str(criterion)}
                    predicate = ui.textarea('앞으로 적용할 조건' if future_scope else '적용할 비교 기준',value=criterion.get('predicate','')).props('outlined autogrow').classes('w-full')
                    effective = ui.input('적용 시작 청구월 (YYYY-MM)',value=pending.get('effective_from_month') or criterion.get('effective_from_month','')).props('outlined placeholder="예: 2029-08"').classes('w-full') if future_scope else None
                    ui.label('입력한 조건은 요청 초안입니다. 아직 검증 기준에 저장하거나 적용하지 않습니다.').classes('muted')
                    def criterion_choice():
                        condition = (predicate.value or '').strip()
                        month = (effective.value or '').strip() if effective else None
                        if not condition or (future_scope and not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',month or '')):
                            ui.notify('적용 조건과 시작 청구월을 입력해 주세요.' if future_scope else '적용할 비교 기준을 입력해 주세요.',type='warning')
                            return
                        value = {'predicate':condition}
                        if future_scope:
                            value['effective_from_month'] = month
                        intake_choice('criterion',value,draft_id)
                    criterion_button = ui.button('적용 조건 확인', on_click=criterion_choice).props('unelevated no-caps')
                    for element in (predicate,effective,criterion_button):
                        if element is not None:
                            element.set_enabled(not running)
                prompt_once(pending.get('message'))
                if status == 'draft_ready':
                    scope_label = {'current_case':'이번 청구만','future_only':'앞으로만 적용','current_and_future':'이번 청구와 앞으로 모두'}.get(pending.get('application'),'이번 청구만' if pending.get('scope')=='once' else '범위 미확정')
                    ui.label('적용 범위 · '+scope_label).classes('text-sm font-medium')
                    if pending.get('reason'):
                        ui.label('처리 사유 · '+str(pending['reason'])).classes('text-sm break-words')
                    criterion = pending.get('criterion')
                    condition = criterion.get('predicate') if isinstance(criterion,dict) else criterion
                    if condition:
                        ui.label('요청한 비교 기준 · '+str(condition)).classes('text-sm break-words')
                    start_month = pending.get('effective_from_month') or (criterion.get('effective_from_month') if isinstance(criterion,dict) else None)
                    if start_month:
                        ui.label('적용 시작 청구월 · '+str(start_month)).classes('text-sm')
                    ui.label('대상·적용 범위 정리 완료 · 예외나 기준은 아직 저장하지 않았습니다.').classes('text-sm font-semibold').style('color:#745319')
                    if memory_supported and (state.get('memory_preview') or {}).get('state') not in ('preview','awaiting_confirmation','saved','saved_pending_application'):
                        ui.button('저장 내용 확인',on_click=lambda did=draft_id:memory_action('prepare',pending_id=did)).props('unelevated no-caps data-testid=prepare-memory-save').set_enabled(not running)
                if status not in ('cancelled','read_only','saved_pending_application') and not any(item.get('choice')=='cancel' for item in choices):
                    ui.button('요청 정리 취소',on_click=lambda did=draft_id:intake_choice('cancel',None,did)).props('flat no-caps').set_enabled(not running)

    def submit(value=None):
        text = (value if isinstance(value, str) else prompt.value or '').strip()
        if adapter.snapshot()['job']['status'] == 'running':
            ui.notify('현재 검증이 실행 중입니다. 완료 후 보내주세요.')
            return
        now = time.monotonic()
        if text and text == last_submit['text'] and now - last_submit['at'] < 0.8:
            return
        last_submit.update(text=text, at=now)
        ui.run_javascript('window.skinChat?.follow()')
        if text:
            try:
                adapter.chat(text)
                prompt.set_value('')
                refresh()
            except Exception as exc:
                ui.notify(str(exc),type='negative')

    prompt.on('compositionstart', js_handler="e => {e.target.dataset.composing = 'true';}")
    prompt.on('compositionend', js_handler="e => {e.target.dataset.composing = 'false';}")
    prompt.on('keydown', lambda e: submit(e.args), js_handler=ENTER_HANDLER)

    filter_sync={'active':False}
    filter_previous={category.id:['전체'],group.id:['전체'],status_filter.id:['확인 필요']}

    def label_filter(field):
        values=filter_values(field.value)
        labels=field.options if isinstance(field.options,dict) else {}
        text=' · '.join(str(labels.get(value,value)) for value in values)
        field.props['display-value']=text
        field.props['title']=text
        field.update()

    def set_filter_value(field,value):
        selected=filter_values(value)
        filter_previous[field.id]=selected[:]
        field.set_value(selected)
        label_filter(field)

    def normalize_filter(field):
        selected=filter_selection(field.value,filter_previous[field.id])
        filter_sync['active']=True
        try:set_filter_value(field,selected)
        finally:filter_sync['active']=False

    def sync_check_filters(category_value=None,group_value=None):
        was_active=filter_sync['active']
        filter_sync['active']=True
        try:
            chosen=filter_values(category.value if category_value is None else category_value)
            categories,_=check_filter_options(view['rows'])
            chosen=[v for v in chosen if v in categories] or ['전체']
            _,checks=check_filter_options(view['rows'],chosen)
            selected=filter_values(group.value if group_value is None else group_value)
            selected=[v for v in selected if v in checks] or ['전체']
            filter_previous[category.id]=chosen[:]
            filter_previous[group.id]=selected[:]
            category.set_options(categories,value=chosen)
            group.set_options(checks,value=selected)
            label_filter(category)
            label_filter(group)
            group.set_visibility('전체' not in chosen)
        finally:filter_sync['active']=was_active

    def apply_filter():
        if filter_sync['active']:return
        view.update(group=group.value, status=status_filter.value, query=query.value or '')
        from review_catalog import filtered,annotate,display_group
        categories=filter_values(category.value)
        candidates=[r for r in view['rows'] if '전체' in categories or check_category(display_group(r)) in categories]
        selected = filtered([r if '_effective_status' in r else annotate(r) for r in candidates], view['group'], view['status'], view['query'])
        boundaries = []
        date_error = False
        for field in (date_from, date_to):
            try:
                boundaries.append(query_date(field.value, int(policy['invoice_month'][:4])))
                field.error = None
            except ValueError as exc:
                boundaries.append(None)
                field.error = str(exc)
                date_error = True
            field.update()
        if not date_error and all(boundaries) and boundaries[0] > boundaries[1]:
            date_to.error = '종료일은 시작일 이후로 선택해 주세요.'
            date_to.update()
            date_error = True
        selected = [] if date_error else filter_dates(selected, *boundaries)
        if view['focus_ids'] is not None:selected=[r for r in selected if r['_id'] in view['focus_ids']]
        from cost_check_context import contextual_rows
        context = view.get('cost_context')
        selected = contextual_rows(selected, context, context_normal.value, view['status'])
        cost_context_bar.set_visibility(bool(context))
        if context:
            label = context['items'][0] if len(context['items']) == 1 else f"청구 {len(context['items'])}개 항목"
            cost_context_label.set_text(f"{context['label']} › {label} · 비용 상세 순서")
        selected = [dict(r, _display_review=review_labels.get(r.get('_review_status', 'unreviewed'), '미처리')) for r in selected]
        checklist_page['rows'] = display_rows(selected)
        checklist_page['lookup'] = {r['_id']:r for r in checklist_page['rows']}
        table.selected=[];bulk_count.set_text('선택 0개')
        show_review_page({**table.pagination,'page':1})
        count_label.set_text(('사용자가 처리완료·예외인정한 항목만 조회 중 · ' if view['status']==['직접 완료'] else '') +
                             (f"해당 비용의 검증 내역 {len(selected):,}개 · 일치는 ‘일치 포함’으로 조회" if context else f"검색 결과 {len(selected):,}행 / 전체 검증 항목 {len(view['rows']):,}개") +
                             ' · 같은 청구에 여러 확인 항목이 있을 수 있습니다.')
        if view['selected'] not in {r['_id'] for r in selected}:
            hide_detail()

    def change_category():
        if filter_sync['active']:return
        normalize_filter(category)
        view['focus_ids']=None
        view['cost_context']=None
        sync_check_filters()
        apply_filter()

    def change_filter(event):
        if filter_sync['active']:return
        if event.sender in (group,status_filter):
            normalize_filter(event.sender)
        # A new explicit filter replaces the dashboard/report shortcut scope.
        view['focus_ids']=None
        view['cost_context']=None
        apply_filter()

    category.on_value_change(change_category)
    group.on_value_change(change_filter)
    status_filter.on_value_change(change_filter)
    query.on_value_change(change_filter)
    date_from.on_value_change(change_filter)
    date_to.on_value_change(change_filter)
    context_normal.on_value_change(apply_filter)

    def select_filter(group_name='전체', state='확인 필요'):
        view['focus_ids']=None
        view['cost_context']=None
        filter_sync['active']=True
        try:
            sync_check_filters('전체' if group_name=='전체' else check_category(group_name),group_name)
            set_filter_value(status_filter,state)
            query.set_value('')
            date_from.set_value('')
            date_to.set_value('')
        finally:filter_sync['active']=False
        apply_filter()
        tabs.set_value(results_tab)

    def report_targets(ids):
        report_dialog.close()
        select_filter()
        view['focus_ids'] = set(ids)
        apply_filter()

    def cost_targets(ids, context=None):
        cost_dialog.close()
        report_dialog.close()
        view['focus_ids'] = set(ids)
        view['cost_context'] = context
        filter_sync['active']=True
        try:
            context_normal.set_value(False)
            sync_check_filters('전체','전체')
            set_filter_value(status_filter,'전체')
            query.set_value('')
            date_from.set_value('')
            date_to.set_value('')
        finally:filter_sync['active']=False
        table.pagination = {**table.pagination, 'sortBy': None, 'descending': False, 'page': 1}
        apply_filter()
        tabs.set_value(results_tab)
        ui.run_javascript(f"document.getElementById('c{tabs.id}')?.scrollIntoView({{behavior:'smooth',block:'start'}})")

    def open_cost_section(data, section, priority='source'):
        from pathlib import PurePosixPath
        from report_overview_ui import open_section_details
        source = data['claim_source']
        section = next((s for s in data['coverage']['sections'] if s['label'] == section['label']), section)
        open_section_details(section, PurePosixPath(source).name, cost_targets,
                             lambda: ui.download(download_url(source)), cost_dialog, priority=priority)

    report_request = {'version': 0}

    async def render_report():
        from report_overview import load_overview
        from report_overview_ui import render_overview
        report_request['version'] += 1
        version = report_request['version']
        reports.clear()
        with reports:
            ui.spinner(size='24px')
        try:
            data = await run.io_bound(load_overview, adapter.root, list(view['rows']))
            if version != report_request['version']:
                return
            reports.clear()
            with reports:
                render_overview(data, report_targets,
                                lambda: ui.download(download_url(REPORT)),
                                lambda: ui.download('/review-status.csv'),
                                lambda section, priority='source': open_cost_section(data, section, priority))
        except (ValueError, OSError, KeyError) as exc:
            if version != report_request['version']:
                return
            reports.clear()
            with reports:
                ui.label(str(exc)).classes('muted')
                ui.button('원문 보고서 다운로드', icon='download',
                          on_click=lambda: ui.download(download_url(REPORT))).props('outline no-caps')

    async def open_report():
        report_dialog.open()
        await render_report()

    def select_all_review():
        table.selected=[{'_id':r['_id']} for r in checklist_page['rows']]
        table.update();bulk_count.set_text(f'선택 {len(table.selected):,}개')

    def clear_review_selection():
        table.selected=[];table.update();bulk_count.set_text('선택 0개')

    async def bulk_review(status):
        if checklist_ui['busy']:return
        ids=[r['_id'] for r in table.selected]
        if not ids:ui.notify('먼저 처리할 항목을 체크해 주세요.',type='warning');return
        token=view.get('review_token');checklist_ui['busy']=True
        for button in (bulk_done,bulk_reset):button.disable()
        try:
            receipt=await run.io_bound(adapter.complete_checklist,ids,status,token)
            ui.notify(f"{receipt['count']:,}개 항목을 "+{'reviewed':'처리완료로 반영했습니다.','review_required':'미처리로 표시했습니다.','reset':'원래 상태로 되돌렸습니다.'}[status],type='positive')
            render_saved();refresh()
        except Exception as exc:ui.notify(str(exc),type='negative')
        finally:
            checklist_ui['busy']=False
            for button in (bulk_done,bulk_reset):button.enable()

    def select_related(ids):
        select_filter()
        view['focus_ids']=set(ids);apply_filter()
        ui.notify(f'질문과 관련된 {len(ids)}개 항목만 표시합니다. 초기화로 전체 목록을 볼 수 있습니다.')

    async def select_catalog_result(result):
        from catalog_query import read_rows, version
        try:
            rows = await run.io_bound(read_rows, adapter.root, result['conversation_month'])
            if version(rows) != result['catalog_result']['version']:
                ui.notify('조회 후 목록이 변경되었습니다. 채팅에서 다시 검색해 주세요.', type='warning')
                return
            select_filter(state='전체')
            view['focus_ids'] = set(result['related_issue_ids'])
            apply_filter()
        except (ValueError, OSError, KeyError) as exc:
            ui.notify('검색 내역을 열지 못했습니다: '+str(exc), type='warning')

    def show_reviewed():
        select_filter(state='직접 완료')

    def hide_detail():
        view['selected'] = None
        table.props[':table-row-class-fn'] = '() => ""'
        table.update()
        detail.set_visibility(False)
        detail_layout.classes(remove='has-selection')

    async def preview_dialog(issue_id, field, group_index):
        chosen = {'index':group_index}
        selected_row = next((r for r in view['rows'] if r['_id']==issue_id), {})
        position = selected_row.get(field, '')
        if field=='청구위치' and selected_row.get('_cost_evidence'):
            position += '; ' + selected_row['_cost_evidence']
        groups = source_groups(position)
        async def choose_sheet(e):
            chosen['index'] = e.value
            await show()
        with ui.dialog() as dialog,ui.card().classes('source-preview-dialog'):
            with ui.row().classes('w-full justify-between items-center'):
                ui.label('원본 근거 미리보기').classes('section-title')
                ui.button(icon='close',on_click=dialog.close).props('flat round')
            ui.label('원본 저장값입니다. 날짜가 숫자로 저장된 셀은 엑셀의 날짜 일련번호로 보일 수 있습니다.').classes('muted')
            if len(groups)>1:
                ui.select({i:g['sheet'] for i,g in enumerate(groups)}, value=group_index,
                          label='조회할 시트', on_change=choose_sheet).classes('w-72').props('data-testid=preview-sheet-select')
            body=ui.column().classes('w-full min-h-0 source-preview-body')
        dialog.open()
        async def show(page=1):
            body.clear()
            with body:ui.spinner();ui.label('해당 근거 행을 읽고 있습니다. 큰 WMS 파일은 시간이 걸릴 수 있습니다.').classes('muted')
            try:
                result=await run.io_bound(adapter.preview,issue_id,field,chosen['index'],page)
                body.clear()
                with body:
                    ui.label(result['file']+' · '+result['sheet']).classes('font-semibold text-sm')
                    columns,width=preview_table_columns(result)
                    source_table=ui.table(columns=columns,rows=preview_display_rows(result),row_key='원본 행',pagination=20).classes('w-full source-preview-table').props('dense data-testid=source-preview-table')
                    source_table.style(f'--preview-min-width:{width}px')
                    source_table.add_slot('header-cell','''<q-th :props="props">{{ props.col.label }}<q-tooltip v-if="props.col.name !== '원본 행'">엑셀 {{ props.col.name }}열</q-tooltip></q-th>''')
                    source_table.add_slot('body-cell','''<q-td :props="props"><span class="preview-cell-text" :title="String(props.value ?? '')">{{ props.value }}</span></q-td>''')
                    if result['missing_rows']:ui.label('원본에 값이 없는 행: '+', '.join(map(str,result['missing_rows']))).classes('muted')
                    ui.pagination(1,result['pages'],value=page,on_change=lambda e:show(e.value)).props('max-pages=7 boundary-links :ellipses="false" :boundary-numbers="false"')
            except Exception as exc:
                body.clear()
                with body:ui.label('근거를 표시하지 못했습니다: '+str(exc)).classes('text-red-600')
        await show()

    def show_issue(row):
        row = display_rows([row])[0]
        view['selected'] = row['_id']
        import json
        active_id = json.dumps(row['_id'], ensure_ascii=True)
        table.props[':table-row-class-fn'] = f'(row) => row._id === {active_id} ? "review-active-row" : ""'
        table.update()
        detail.clear()
        detail_layout.classes(add='has-selection')
        detail.set_visibility(True)
        with detail:
            with ui.row().classes('w-full items-start justify-between flex-nowrap'):
                with ui.column().classes('gap-1'):
                    ui.label('선택한 내역').classes('muted')
                    ui.label(row['_display_group']).classes('section-title')
                ui.button(icon='close', on_click=hide_detail).props('flat round dense aria-label="상세 닫기"').tooltip('상세 닫기')
            shown=row.get('_effective_status',row['결과'])
            ui.label(row.get('_display_status') or shown).classes('status-pill').style("background:#EEE5D9;color:#75604A" if shown == "잠정 확정" else f"background:{STATUS_COLORS.get(shown, '#6B7684')}15;color:{STATUS_COLORS.get(shown, '#6B7684')}")
            if (shown!=row['결과'] or row.get('_normal_basis')) and not row.get('_evidence_review'):
                ui.label((row.get('_normal_basis') or '담당자 확인 필요')+' · 원래 표시: '+row['결과']).classes('muted')
            if disposition_supported:
                ui.label('담당자 처리 · ' + review_labels.get(row.get('_review_status', 'unreviewed'), '미처리')).classes('text-sm font-semibold').props('data-testid=issue-review-status')
                if row.get('_review_reason'):
                    ui.label('처리 사유 · ' + str(row['_review_reason'])).classes('text-sm break-words')
                if row.get('_review_status') != 'unreviewed' and row.get('_review_status'):
                    ui.label('원래 계산값과 검증 판정은 유지합니다.').classes('muted')
            ui.label(row['_display_target']).classes('section-title')
            ui.label(row['_display_target_kind']).classes('muted')
            target_action['button'] = ui.button('이 건으로 대화',icon='chat_bubble_outline',on_click=lambda r=row:chat_about_issue(r)).props('outline no-caps data-testid=chat-about-issue').classes('w-full')
            target_action['button'].set_enabled(adapter.snapshot()['job']['status'] != 'running')
            if str(row['_id']).startswith('normal:'):
                target_action['button'].set_visibility(False)
            if row.get('청구원본'):
                ui.label('청구 파일: ' + row['_display_source']).classes('text-xs break-all')
            for field in ('일자','주문번호','입고예정번호','품번','송장번호'):
                if row.get(field):
                    with ui.column().classes('gap-1 w-full'):
                        ui.label(field).classes('muted')
                        ui.label(row[field]).classes('text-sm font-medium break-all')
            with ui.element('div').classes('value-grid'):
                from calculation_view import comparison_label
                for field, label in [('청구값','청구'),('비교값',comparison_label(row)),('차이','차이')]:
                    with ui.element('div').classes('value-cell'):
                        ui.label(label).classes('muted value-label')
                        ui.label(format_number(row.get(field))).classes('number w-full').tooltip(format_number(row.get(field))).style('color:#E5484D' if field=='차이' and row.get(field) not in ('','0',None) else '')
            ui.label('단위: ' + (row.get('단위') or '원본에 기재 없음')).classes('muted')
            if row.get('_rule_event') or row.get('_rule_status'):
                with ui.column().classes('command-target gap-2').props('data-testid=issue-rule-comparison'):
                    ui.label('새 기준으로 계산한 결과').classes('text-sm font-semibold')
                    ui.label('새 기준 판정 · ' + rule_labels.get(row.get('_rule_status'), str(row.get('_rule_status') or '확인 필요'))).classes('text-sm')
                    if row.get('_rule_comparison') not in (None, ''):
                        ui.label('새 비교값 · ' + format_number(row['_rule_comparison']) + ' ' + str(row.get('단위') or '')).classes('text-sm')
                    if row.get('_rule_difference') not in (None, ''):
                        ui.label('새 차이 · ' + format_number(row['_rule_difference']) + ' ' + str(row.get('단위') or '')).classes('text-sm')
                    if row.get('_rule_invalidated'):
                        ui.label('자료 또는 기준이 바뀌어 이전 적용 결과를 사용하지 않습니다. 현재 근거로 다시 확인해 주세요.').classes('text-sm').style('color:#a86a08')
                    if row.get('_rule_basis'):
                        ui.label('적용 기준 · ' + str(row['_rule_basis'])).classes('text-sm break-words')
                    if row.get('_rule_evidence'):
                        with ui.expansion('새 계산의 원본 근거').classes('w-full'):
                            for ref in row['_rule_evidence']:
                                ui.label(str(ref)).classes('muted break-all')
                    ui.label('위 원래 계산값·판정은 유지합니다. 선택한 항목의 비교 결과이며 전체 금액 승인이 아닙니다.').classes('muted')
            from calculation_view import render_brief
            render_brief(row)
            if row.get('검사')=='B2C 주문 연결':
                async def show_calculation():
                    with ui.dialog() as dialog,ui.card().classes('w-[650px] max-w-full gap-3').props('data-testid=calculation-detail'):
                        ui.label('계산·연결 과정').classes('section-title')
                        body=ui.column().classes('w-full')
                        ui.button('닫기',on_click=dialog.close).props('flat')
                    dialog.open()
                    try:
                        from calculation_view import explain
                        values=await run.io_bound(explain,adapter.root,row)
                        with body:
                            for name,value in values:
                                ui.label(name).classes('muted');ui.label(value).classes('text-sm break-words')
                    except Exception as exc:
                        with body:ui.label(str(exc)).classes('text-red-600')
                ui.button('연결 조사 내용 보기',on_click=show_calculation).props('outline no-caps data-testid=explain-calculation')
            invoice_position = row.get('청구위치','') + ('; ' + row['_cost_evidence'] if row.get('_cost_evidence') else '')
            render_locations('청구서에서 찾기', invoice_position,lambda index:preview_dialog(row['_id'],'청구위치',index))
            render_locations('WMS에서 찾기', row.get('WMS위치'),lambda index:preview_dialog(row['_id'],'WMS위치',index))
            if not source_groups(row.get('WMS위치')):
                with ui.column().classes('w-full gap-1').props('data-testid=wms-source-unavailable'):
                    ui.label('연결된 WMS 내역 없음').classes('muted text-xs')
            if row.get('검사','').startswith('B2C WMS 포장') or 'B2C WMS 포장' in row.get('_cost_checks',''):
                with ui.expansion('포장 계산에 사용한 WMS 원본',icon='description').classes('w-full'):
                    try:
                        from calculation_view import packing_sources
                        for source in packing_sources(adapter.root):
                            ui.link(source['label']+' · '+Path(source['path']).name,download_url(source['path'])).classes('text-sm')
                            if source.get('sheet'):ui.label('시트: '+source['sheet']).classes('muted text-sm')
                    except (ValueError,KeyError,OSError) as exc:ui.label(str(exc)).classes('muted text-sm')
            paths = {row.get('청구원본', '')}
            paths.update(re.findall(r'(data/demo_company/source/[^;]+?) / ', row.get('WMS위치', '')))
            for relative in sorted(paths):
                try:
                    file = safe_file(ROOT, relative)
                    ui.link('원본 다운로드 · ' + file.name, download_url(relative)).classes('text-xs break-all')
                except ValueError:
                    pass
        # On narrow layouts the selected card is below the table; bring it into view.
        ui.run_javascript(f'if (window.innerWidth < 1250) document.getElementById("c{detail.id}")?.scrollIntoView({{behavior:"smooth",block:"start"}})')

    def render_saved():
        raw = (ROOT / REPORT).read_text(encoding='utf-8') if (ROOT / REPORT).exists() else ''
        summary = summary_rows(raw)
        view['rows'] = adapter.review_items() if hasattr(adapter,'review_items') else adapter.issues()
        if bulk_supported:view['review_token']=adapter.review_token(view['rows'])
        if hasattr(adapter,'review_items'):
            from review_catalog import summary as review_summary
            summary=review_summary(view['rows'])
        sync_check_filters()
        apply_filter()
        if view['selected'] is not None:
            selected_row = next((r for r in view['rows'] if r['_id'] == view['selected']), None)
            if selected_row is None:
                hide_detail()
            else:
                show_issue(selected_row)
        dashboard.clear()
        if dashboard_variant == 'priority':
            from priority_dashboard import render_dashboard
            from report_overview import load_overview
            from review_catalog import coverage
            stamp = datetime.fromtimestamp((ROOT / REPORT).stat().st_mtime, timezone(timedelta(hours=9))).strftime('%Y.%m.%d %H:%M') if raw else None
            with dashboard:
                try:
                    financial_data = load_overview(adapter.root, view['rows'])
                    render_dashboard(
                        financial_data, summary, adapter.root, stamp,
                        coverage(adapter.root, view['rows']),
                        show_rows=report_targets, show_filter=select_filter,
                        show_section=lambda section: open_cost_section(financial_data, section),
                        show_report=open_report, request_verification=choose_chat,
                    )
                except (ValueError, OSError, KeyError) as exc:
                    ui.label(f'저장된 검증 요약을 표시할 수 없습니다: {exc}').classes('notice')
                    ui.link('기존 화면 열기', '/').classes('text-primary')
            if report_dialog.value:
                background_tasks.create(render_report())
            return
        with dashboard:
            with ui.row().classes('w-full items-start justify-between gap-3'):
                with ui.column().classes('gap-2'):
                    ui.label('청구 검증 현황').classes('title')
                    ui.label('DEMO COMPANY · ' + scope).classes('scope-line text-sm font-medium')
                    ui.label(period_text).classes('muted')
                with ui.row().classes('items-center gap-2'):
                    ui.button('검증 요청', icon='add', on_click=choose_chat).props('unelevated no-caps')
            from report_overview import load_overview
            from report_overview_ui import render_financial_dashboard
            try:
                financial_data = load_overview(adapter.root, view['rows'])
            except (ValueError, OSError, KeyError) as exc:
                financial_data = None
                ui.label(f'청구금액 요약을 표시할 수 없습니다: {exc}').classes('notice text-sm')
            if financial_data is not None:
                render_financial_dashboard(financial_data, report_targets,
                                           lambda s, data=financial_data: open_cost_section(data, s), open_report,
                                           classic=dashboard_variant == 'classic')
            else:
                ui.button('보고서 보기', icon='description', on_click=open_report).props('outline no-caps data-testid=open-dashboard-report')
                with ui.element('div').classes('metric-grid'):
                    for card in dashboard_cards(summary):
                        with ui.card().classes('metric-card').props(f'role=button tabindex=0 aria-label="{card["title"]}"') as tile:
                            ui.label(card['title']).classes('text-sm font-semibold')
                            ui.label(format_number(card['count'])).classes('metric-number').style(f'color:{STATUS_COLORS[card["status"]]}')
                            ui.label(card['unit']).classes('muted')
                        tile.on('click', lambda c=card: select_filter(c['group'],c['status']))
                        tile.on('keydown.enter', lambda c=card: select_filter(c['group'],c['status']))
            with ui.element('div').classes('dashboard-analysis-grid').props('data-testid=dashboard-analysis-grid'):
                from monthly_trend import render_trend
                render_trend(adapter.root, policy.get('invoice_month'),
                             financial_data['totals']['net'] if financial_data is not None else None,
                             {s['label']:s['amount'] for s in financial_data['billed_sections']} if financial_data is not None else None)
                if dashboard_variant == 'classic':
                    with ui.card().classes('w-full gap-2').props('data-testid=verification-status-card'):
                        with ui.row().classes('w-full items-center justify-between gap-2'):
                            ui.label('항목별 검증 현황').classes('section-title')
                            with ui.row().classes('items-center gap-3'):
                                for status, color in STATUS_COLORS.items():
                                    ui.label('● ' + status).classes('status-count').style(f'color:{color}')
                        ui.label('서로 다른 기준으로 집계하며, 정상에는 담당자 처리완료도 포함됩니다.').classes('muted')
                        for row in summary:
                            if not row['불일치'] and not row['미검증']:
                                continue
                            title, unit = summary_presentation(row)
                            with ui.column().classes('status-row gap-2').props(
                                    f'role=button tabindex=0 data-history-view=checklist aria-label="{title} 상세"') as tile:
                                with ui.row().classes('w-full items-center justify-between gap-2'):
                                    with ui.column().classes('gap-0'):
                                        ui.label(title).classes('font-semibold text-sm')
                                        ui.label(unit).classes('muted')
                                    with ui.row().classes('items-center gap-2'):
                                        for status, color in STATUS_COLORS.items():
                                            ui.label(f'{status} {row[status]:,}').classes('status-count').style(f'color:{color}')
                                        ui.icon('arrow_forward', color='primary', size='18px')
                                with ui.element('div').classes('status-track'):
                                    for status, percent in shares(row).items():
                                        ui.element('div').style(f'width:{percent}%;height:100%;background:{STATUS_COLORS[status]}')
                            tile.on('click', lambda group=row['group']: select_filter(group, '전체'))
                            tile.on('keydown.enter', lambda group=row['group']: select_filter(group, '전체'))
                            tile.on('keydown.space.prevent', lambda group=row['group']: select_filter(group, '전체'))
                elif financial_data is not None:
                    from report_overview_ui import render_cost_composition
                    render_cost_composition(financial_data, lambda s: open_cost_section(financial_data, s))
                else:
                    with ui.card().classes('w-full gap-2'):
                        ui.label('항목별 청구 물류비').classes('section-title')
                        ui.label('저장된 청구금액 요약을 불러올 수 없습니다.').classes('muted')
            if hasattr(adapter,'review_items'):
                from review_catalog import coverage
                coverage_rows=coverage(adapter.root,view['rows'])
                def coverage_content():
                    ui.label('원본 행에는 제목·합계가 포함됩니다. 실제 대사는 날짜·번호·품번별로 합산하므로 원본 행 수와 비교 대상 수가 다릅니다. 상품 개수(EA)가 아닙니다.').classes('muted')
                    ui.table(columns=[{'name':k,'field':k,'label':v,'align':'right' if k in ('raw_rows','targets') else 'left'} for k,v in [('label','구분'),('sheet','청구 시트'),('raw_rows','값이 있는 원본 행'),('targets','비교 대상 수'),('unit','대상 1개의 의미')]],rows=coverage_rows,row_key='group').props('flat data-testid=coverage-table').classes('w-full')
                with ui.card().classes('w-full gap-3' + ('' if dashboard_variant == 'classic' else ' coverage-collapsible')).props('data-testid=verification-coverage'):
                    if dashboard_variant == 'classic':
                        ui.label('전체 검증 범위 · DEMO COMPANY 가상센터 2029년 7월').classes('section-title')
                        coverage_content()
                    else:
                        with ui.expansion('전체 검증 범위 · DEMO COMPANY 가상센터 2029년 7월', value=False).classes('w-full coverage-expansion').props('data-testid=coverage-toggle'):
                            with ui.column().classes('w-full coverage-content'):
                                coverage_content()
            with ui.row().classes('w-full items-center justify-between'):
                stamp = datetime.fromtimestamp((ROOT / REPORT).stat().st_mtime, timezone(timedelta(hours=9))).strftime('%Y.%m.%d %H:%M') if raw else '없음'
                ui.label('최신 저장 보고서 · ' + stamp + ' KST').classes('muted')
                ui.button('전체 확인 목록', icon='arrow_forward', on_click=lambda: select_filter()).props('flat no-caps')
        if report_dialog.value:
            background_tasks.create(render_report())

    def refresh():
        state = adapter.snapshot()
        update_verification_notice(state)
        job = state['job']
        if last_library['revision']!=state['library_revision']:
            last_library['revision']=state['library_revision']
            chat_title.set_text(state['chat_title'])
        if last_library['chat_id']!=state['chat_id']:
            last_library['chat_id']=state['chat_id'];selected_messages.clear()
            message_checkboxes.clear()
            delete_selected.set_text('선택 삭제 (0)')
            set_message_selection_mode(False)
        same_chat=job.get('conversation_id',state['chat_id'])==state['chat_id']
        running = job['status'] == 'running'
        intake_running = running or state.get('verification_job',{}).get('status') == 'running' or memory_ui['busy'] or disposition_ui['busy'] or rule_ui['busy']
        intake_signature = (state['chat_id'],str(state.get('chat_selection')),str(state.get('command_intake')),str(state.get('memory_preview')),str(state['messages'][-1:]),intake_running)
        if last['intake'] != intake_signature:
            last['intake'] = intake_signature
            render_command_intake(state,intake_running)
        memory_signature=(state['chat_id'],str(state.get('memory_preview')),str(state.get('disposition_preview')),str(state.get('rule_preview')),intake_running,memory_ui['message'])
        if last['memory'] != memory_signature:
            last['memory']=memory_signature
            render_memory_preview(state,intake_running)
        disposition_signature = (state['chat_id'], str(state.get('disposition_preview')), intake_running, disposition_ui['message'])
        if last['disposition'] != disposition_signature:
            last['disposition'] = disposition_signature
            render_disposition_preview(state, intake_running)
        rule_signature = (state['chat_id'], str(state.get('rule_preview')), intake_running, rule_ui['message'])
        if last['rule'] != rule_signature:
            last['rule'] = rule_signature
            render_rule_preview(state, intake_running)
        if target_action['button'] is not None:
            target_action['button'].set_enabled(not intake_running)
        send.set_enabled(not intake_running)
        is_chat = job.get('action') == 'chat'
        elapsed = int(time.time() - job.get('started_at', time.time()))
        progress.set_text(f'실행 중 · {elapsed // 60}분 {elapsed % 60}초 경과 · 창을 새로고침해도 작업은 계속됩니다.' if running else '현재 등록 범위의 검증 요청 · 새 기준은 사용자 답변 후 반영합니다.')
        if is_chat:
            progress.set_text('응답을 기다리고 있습니다.' if running else '자연어 대화 · 원본 파일 대신 등록 상태와 검증 요약을 참조합니다.')
        signature = (state['chat_id'],str(state['messages']), job.get('id'), job['status'])
        if last['signature'] != signature:
            last['signature'] = signature
            # Retain existing message elements: status polling must not replay or move history.
            incoming = state['messages']
            previous = [item[0] for item in rendered_messages]
            keep = min(len(previous), len(incoming))
            while keep and previous[-keep:] != incoming[:keep]:
                keep -= 1
            for old_message, element in rendered_messages[:len(previous)-keep]:
                message_checkboxes.pop(old_message['id'],None)
                selected_messages.discard(old_message['id'])
                element.delete()
            del rendered_messages[:len(previous)-keep]
            with messages:
                for message in incoming[keep:]:
                    sent = message['role'] == 'user'
                    with ui.column().classes('chat-entry gap-0') as element:
                        with ui.row().classes('message-actions w-full justify-end items-center gap-1'):
                            message_checkboxes[message['id']]=ui.checkbox('선택',on_change=lambda e,key=message['id']:select_message(key,e.value)).classes('message-selector').props('dense')
                            message_checkboxes[message['id']].set_visibility(message_selection_mode['active'])
                            ui.button(icon='delete_outline',on_click=lambda key=message['id']:delete_messages([key])).props('flat round dense aria-label=메시지삭제').tooltip('이 메시지 삭제')
                        ui.chat_message(message['text'] if sent else display_explanation(message['text']), name='나' if sent else '물류비 청구 검증 에이전트', sent=sent).props(f'bg-color={"chat-sent" if sent else "chat-received"} text-color=dark').classes('w-full')
                    if previous:
                        element.classes('chat-new')
                    rendered_messages.append((message, element))
            update_message_selection()
            chat_status.clear()
            with chat_status:
                chat_result=state.get('chat_result',{})
                if chat_result.get('evidence_mode')=='stored_result':
                    ui.label('저장 결과 설명 · 이번 답변에서 재대사를 실행하지 않았습니다.').classes('muted text-sm')
                if not incoming:
                    with ui.card().classes('w-full items-center py-12 gap-4'):
                        ui.icon('auto_awesome', size='32px', color='primary')
                        ui.label('무엇을 도와드릴까요?').classes('text-2xl font-semibold')
                        ui.label('원본 자료를 대조하고, 차이와 확인할 근거를 정리합니다.').classes('muted')
                options=state.get('chat_result',{}).get('suggestions',[])
                if not incoming or state.get('chat_result',{}).get('context_source')=='인사 응답':
                    options=['start_verification','explain_issue','register_month']
                with ui.row().classes('gap-2'):
                    for option in options:
                        if option=='start_verification':ui.button('물류비 검증 시작',on_click=start_verification).props('unelevated no-caps')
                        elif option=='show_results':ui.button('검증 결과 보기',on_click=lambda:tabs.set_value(dashboard_tab)).props('outline no-caps')
                        elif option=='register_month':ui.button('월별 자료 등록',on_click=lambda:tabs.set_value(monthly_tab)).props('outline no-caps')
                        elif option=='open_library':ui.button('이전 대화 찾기',on_click=lambda:tabs.set_value(library_tab)).props('outline no-caps')
                        elif option=='manage_rules':ui.button('기준 선택·이력 보기',on_click=open_rule_management).props('outline no-caps data-testid=manage-rules')
                        elif option=='explain_issue':
                            related=state.get('chat_result',{}).get('related_issue_ids',[])
                            ui.button('질문 관련 문제 보기' if related else '확인할 문제 보기',on_click=lambda ids=related:select_related(ids) if ids else select_filter()).props('outline no-caps')
                        elif option=='ask_question':ui.button('질문 예시 보기',on_click=question_examples).props('outline no-caps')
                        elif option=='catalog_results':
                            count=chat_result.get('catalog_result',{}).get('matched_count',0)
                            ui.button(f'해당 내역 {count:,}건 보기',on_click=lambda r=chat_result:select_catalog_result(r)).props('outline no-caps data-testid=catalog-results')
                if not same_chat:
                    if running:ui.label('다른 채팅에서 실행 중입니다. 결과 답변은 요청한 채팅에 저장됩니다.').classes('muted')
                elif is_chat and job['status']=='completed':
                    month=job.get('result',{}).get('proposed_verification_month')
                    if month:
                        ui.button(month+' 자료로 검증 실행',on_click=lambda m=month:launch('agent',request=f'DEMO COMPANY 가상센터 {m} 전체 청구 검증해줘')).props('unelevated no-caps')
                elif job['status'] == 'completed':
                    with ui.row().classes('gap-2'):
                        if job.get('month'):
                            ui.button('해당 월 결과 조회', icon='calendar_month', on_click=lambda: tabs.set_value(monthly_tab)).props('flat no-caps')
                        else:
                            ui.button('결과 대시보드', icon='space_dashboard', on_click=lambda: tabs.set_value(dashboard_tab)).props('flat no-caps')
                            ui.button('상세 근거 조회', icon='rule', on_click=lambda: select_filter()).props('flat no-caps')
                elif job['status'] == 'failed':
                    ui.label('자연어 응답을 받지 못했습니다. 검증 결과는 유지됩니다.' if is_chat else '마지막 검증 실행에 실패했습니다. 마지막 완료 보고서는 보존되어 있습니다.').classes('text-sm text-red-600')
                elif running:
                    with ui.row().classes('items-center gap-3'):
                        ui.spinner(size='20px')
                        ui.label('응답을 기다리고 있습니다.' if is_chat else '검증 도구의 실행 결과를 기다리는 중입니다.').classes('muted')
            investigation.clear()
            if job.get('result',{}).get('investigation'):
                with investigation, ui.expansion('기존 원인 조사 결과 · 전체 범위', icon='manage_search').classes('report-section'):
                    ui.table(columns=[{'name':'label','label':'항목','field':'label','align':'left'},
                                      {'name':'reason','label':'기록된 설명·판단 범위','field':'reason','align':'left'}],
                             rows=[{'label':f['label'],'reason':f['reason']} for f in job['result'].get('findings',[])],row_key='label').classes('w-full')
        qsig = (str(state['questions']), running)
        if last['questions'] != qsig:
            last['questions'] = qsig
            questions.clear()
            with questions:
                if state['questions']:
                    with ui.card().classes('w-full gap-4').style('background:#fff8ea'):
                        ui.label('사용자 답변이 필요합니다').classes('section-title')
                        answers = {}
                        for q in state['questions']:
                            ui.label(display_explanation(q.get('text',q.get('category','검증 기준'))))
                            if q.get('evidence'):
                                with ui.expansion('질문의 근거').classes('w-full'):
                                    ui.label(display_explanation(q['evidence'])).classes('muted')
                            answers[q['id']] = ui.textarea('답변 · 확정할 수 없으면 명시해 주세요').props('outlined autogrow').classes('w-full')
                        ui.button('답변 저장하고 재개', on_click=lambda: launch('agent',request='재개', answers={k:v.value for k,v in answers.items()})).set_enabled(not running)
        files = tuple((ROOT/p).stat().st_mtime_ns if (ROOT/p).exists() else None for p in (REPORT,BASE/'검토필요_전체목록.csv')) + (state.get('disposition_revision'),state.get('rule_revision'))
        if last['files'] is None or (not running and files != last['files']):
            last['files'] = files
            render_saved()

    refresh()
    ui.timer(0.2, lambda: ui.run_javascript(SCROLL_SCRIPT), once=True)
    ui.timer(1.0, refresh)
