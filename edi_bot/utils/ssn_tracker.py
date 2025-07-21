import re
import urllib
from datetime import datetime, date
import pandas as pd
import pyodbc
from sqlalchemy import create_engine, text

# ---------------------------
# CONFIG
# ---------------------------
SERVER   = r"NIDHI\SQLEXPRESS"
DATABASE = "edi 834"  # TODO: change
TABLE_PATTERN = r"^OOE\d{14}$"  # regex against sys.tables.name
SSN_COL = "SSN"  # column name in your daily tables
# ---------------------------

# --- Build engine (Trusted Connection) ---
params = urllib.parse.quote_plus(
    fr"DRIVER={{ODBC Driver 17 for SQL Server}};"
    fr"SERVER={SERVER};"
    fr"DATABASE={DATABASE};"
    r"Trusted_Connection=yes;"
)
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={params}")

# --- Get daily table names from SQL Server ---
with engine.connect() as conn:
    tbls = pd.read_sql(
        "SELECT name FROM sys.tables ORDER BY name;",
        conn
    )['name'].tolist()

# Filter by pattern
daily_tables = [t for t in tbls if re.match(TABLE_PATTERN, t)]
if not daily_tables:
    raise RuntimeError("No daily tables found matching pattern.")

# Parse date from table name (first 8 digits after OOE)
def table_date(table_name: str) -> date:
    m = re.search(r"OOE(\d{8})", table_name)
    if not m:
        raise ValueError(f"Cannot parse date from table {table_name}")
    return datetime.strptime(m.group(1), "%Y%m%d").date()

# Sort tables oldest → newest
daily_tables = sorted(daily_tables, key=table_date)

# --- Helper: normalize SSN to 9 digits ---
def normalize_ssn(x):
    if pd.isna(x):
        return None
    digits = re.sub(r"\D", "", str(x))
    return digits if len(digits) == 9 else None

# --- Pull distinct SSNs from each daily table ---
presence_frames = []
with engine.connect() as conn:
    for tbl in daily_tables:
        d = table_date(tbl)
        sql = text(f"SELECT DISTINCT [{SSN_COL}] FROM [{tbl}] WHERE [{SSN_COL}] IS NOT NULL;")
        tmp = pd.read_sql(sql, conn)
        tmp['SSN_norm'] = tmp[SSN_COL].apply(normalize_ssn)
        tmp = tmp.dropna(subset=['SSN_norm']).drop_duplicates(subset=['SSN_norm'])
        tmp = tmp[['SSN_norm']].copy()
        tmp['file_date'] = d
        tmp['table_name'] = tbl
        presence_frames.append(tmp)

presence_df = pd.concat(presence_frames, ignore_index=True)

# --- Pivot to build presence matrix (one col per day) ---
presence_matrix = (
    presence_df.assign(present=1)
               .pivot_table(index='SSN_norm', columns='file_date', values='present',
                            aggfunc='max', fill_value=0)
               .sort_index(axis=1)  # ensure chronological
)

# Keep ordered date list
date_cols = list(presence_matrix.columns)
latest_date = date_cols[-1]

# --- Classification function ---
def classify_presence(row):
    """
    row: Series like {d1:0/1, d2:1/0, ...}
    return dict: classification, first_seen, last_seen, reinstated_on, terminated_on, active
    """
    pres = row.values.tolist()
    dates = date_cols

    # indices where present
    present_idx = [i for i, v in enumerate(pres) if v == 1]
    if not present_idx:
        return {
            "classification": "Not Found",
            "active": False,
            "first_seen": None,
            "last_seen": None,
            "terminated_on": None,
            "reinstated_on": None,
            "new_on": None,
        }

    first_seen = dates[present_idx[0]]
    last_seen = dates[present_idx[-1]]

    latest_present = pres[-1] == 1  # present in latest day?

    # detect gaps
    # expected continuous run from first_seen to last_seen:
    continuous = all(pres[i] == 1 for i in range(present_idx[0], present_idx[-1] + 1))

    if latest_present:
        if present_idx[0] == len(pres) - 1:  # only in latest day
            return {
                "classification": "New",
                "active": True,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "terminated_on": None,
                "reinstated_on": None,
                "new_on": latest_date,
            }
        if continuous:
            return {
                "classification": "Active",
                "active": True,
                "first_seen": first_seen,
                "last_seen": latest_date,
                "terminated_on": None,
                "reinstated_on": None,
                "new_on": None,
            }
        # gap exists: reinstated
        # reinstated_on = first date index AFTER most recent gap
        # find last 0 between two 1s
        gap_after = None
        term_date = None
        
        for i in range(1, len(pres)):
            if pres[i-1] == 1 and pres[i] == 0:
                term_date = dates[i]
            elif pres[i-1] == 0 and pres[i] == 1:
                gap_after = dates[i]
        
        return {
            "classification": "Reinstated",
            "active": True,
            "first_seen": first_seen,
            "last_seen": latest_date,
            "terminated_on": term_date,
            "reinstated_on": gap_after if gap_after else latest_date,
            "new_on": None,
        }

    else:
        # Not in latest day -> terminated
        # Choose termination date rule:
        #   A) Use last_seen (last day we saw them)
        #   B) Use first missing day after last_seen (requires scanning)
        # We'll use A (simpler). If you want B, compute below.
        # find first missing day after last_seen index:
        last_idx = present_idx[-1]
        if last_idx < len(pres) - 1:
            terminated_effective = dates[last_idx + 1]  # first missing file date
        else:
            terminated_effective = last_seen  # shouldn't hit because latest_present False
        return {
            "classification": "Terminated",
            "active": False,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "terminated_on": terminated_effective,
            "reinstated_on": None,
            "new_on": None,
        }

# --- Apply classification to all SSNs ---
summary_records = []
for ssn, row in presence_matrix.iterrows():
    info = classify_presence(row)
    info['SSN_norm'] = ssn
    summary_records.append(info)

summary_df = pd.DataFrame(summary_records)

# Optional: join back a "pretty SSN" with dashes if you like
summary_df['SSN'] = summary_df['SSN_norm'].str.replace(r'(\d{3})(\d{2})(\d{4})', r'\1-\2-\3', regex=True)

# Reorder columns
summary_df = summary_df[
    ['SSN', 'SSN_norm', 'classification', 'active',
     'first_seen', 'last_seen', 'terminated_on', 'reinstated_on', 'new_on']
]



def get_ssn_history(ssn_input: str):
    norm = normalize_ssn(ssn_input)
    if norm not in summary_df['SSN_norm'].values:
        return f"SSN {ssn_input} not found in history."
    row = summary_df.loc[summary_df['SSN_norm'] == norm].iloc[0]

    lines = [
        f"SSN: {row['SSN']}",
        f"Classification: {row['classification']}",
        f"Currently Active: {'Yes' if row['active'] else 'No'}",
    ]
    if pd.notna(row['first_seen']):
        lines.append(f"First Seen: {row['first_seen']}")
    if pd.notna(row['last_seen']):
        lines.append(f"Last Seen: {row['last_seen']}")
    if pd.notna(row['new_on']):
        lines.append(f"New On: {row['new_on']}")
    if pd.notna(row['reinstated_on']):
        lines.append(f"Reinstated On: {row['reinstated_on']}")
    if pd.notna(row['terminated_on']):
        lines.append(f"Terminated Effective: {row['terminated_on']}")
    return "\n".join(lines)

def load_summary():
    return summary_df

