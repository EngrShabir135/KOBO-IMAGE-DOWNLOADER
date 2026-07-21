import streamlit as st
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse
import os
import mimetypes
import time
import threading
from io import BytesIO
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import filetype


# -------------------------------------------------------
# Helper functions
# -------------------------------------------------------

def sanitize_name(s):
    """Clean folder names (spaces -> underscore)."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return "UNKNOWN"
    clean = "".join(c for c in str(s) if c.isalnum() or c in (' ', '_', '-')).strip()
    clean = clean.replace(" ", "_")
    return clean if clean else "UNKNOWN"


def sanitize_store_name(s):
    """Clean store name for filenames, spaces -> dash."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return "UNKNOWN"
    clean = "".join(c for c in str(s) if c.isalnum() or c in (' ', '-', '_')).strip()
    clean = clean.replace(" ", "-")
    return clean if clean else "UNKNOWN"


def format_date_folder(val):
    """Format a date value into a DD-MM-YYYY folder name."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "UNKNOWN_DATE"
    try:
        dt = pd.to_datetime(val)
        return dt.strftime('%d-%m-%Y')
    except Exception:
        return sanitize_name(val)


def detect_extension(content, content_type, url):
    """Detect proper file extension."""
    kind = filetype.guess(content)
    if kind:
        return kind.extension
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(';')[0].strip())
        if guessed:
            return guessed.lstrip('.')
    path = urlparse(url).path
    ext2 = os.path.splitext(path)[1]
    if ext2 and len(ext2) <= 6:
        return ext2.lstrip('.')
    return 'jpg'


# Each worker thread gets its own requests.Session (safer than sharing
# one Session object across threads, which requests does not officially
# guarantee to be thread-safe).
_thread_local = threading.local()


def get_session(username, password):
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.auth = HTTPBasicAuth(username, password)
        _thread_local.session = s
    return _thread_local.session


def download_one(username, password, url, dest_name, folder, timeout=20, max_retries=2):
    """Download a single file with retries. Returns (success, final_name, error)."""
    session = get_session(username, password)
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, stream=True, timeout=timeout)
            if resp.status_code == 200:
                content = resp.content
                content_type = resp.headers.get('Content-Type', '')
                ext = detect_extension(content, content_type, url)
                final_name = f"{dest_name}.{ext}"
                final_path = os.path.join(folder, final_name)
                with open(final_path, 'wb') as f:
                    f.write(content)
                return True, final_name, None
            else:
                last_exc = f'HTTP {resp.status_code}'
        except Exception as e:
            last_exc = str(e)
        time.sleep(0.5 * (attempt + 1))
    return False, None, last_exc


def dedupe_columns(columns):
    """Make duplicate column names unique (e.g. 'MT LINK', 'MT LINK' ->
    'MT LINK', 'MT LINK__2') so df[col] always returns a scalar Series,
    never a DataFrame."""
    seen = {}
    new_cols = []
    for c in columns:
        if c not in seen:
            seen[c] = 1
            new_cols.append(c)
        else:
            seen[c] += 1
            new_cols.append(f"{c}__{seen[c]}")
    return new_cols


# -------------------------------------------------------
# Streamlit App
# -------------------------------------------------------

st.title("📊 Download images from KOBO")
st.write("This app downloads images from KOBO and organizes them as: Date → Link Column → images.")
st.write("Expected columns: DATE, Select City Name, STORE NAME, STORE LINK, PEP LINK 1-3, "
         "KO LINK 1-3, OTHER LINK 1-3 / OTHERS LINK 2-3, MT LINK, ID")
st.write("Image file names are generated as: **CityName_Store-Name_ID** (store name spaces become dashes).")

# Username and Password
username = st.text_input('Kobo Username', '')
password = st.text_input('Kobo Password', type='password')

concurrency = st.slider('Concurrent downloads', min_value=1, max_value=10, value=3)
timeout = st.number_input('Request timeout (seconds)', value=20, min_value=5, max_value=120)
max_retries = st.number_input('Max retries per URL', value=2, min_value=0, max_value=5)

uploaded_file = st.file_uploader(
    'Upload Excel or CSV file with links',
    type=['xlsx', 'xls', 'csv']
)

if uploaded_file is not None and username and password:
    try:
        uploaded_file.seek(0)
        if uploaded_file.name.endswith(('.xls', '.xlsx')):
            df = pd.read_excel(uploaded_file)
        else:
            df = pd.read_csv(uploaded_file)
    except Exception as e:
        st.error(f'Error reading file: {e}')
        st.stop()

    # Normalize column names (strip stray whitespace) and de-duplicate
    # any repeated header names so df[col] never returns a DataFrame.
    df.columns = dedupe_columns([str(c).strip() for c in df.columns])

    st.markdown('**Preview of file**')
    st.dataframe(df.head(50))

    required_cols = ["DATE", "Select City Name", "STORE NAME", "ID"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        st.error(f"Error: Missing required column(s): {', '.join(missing)}. Please check header names.")
        st.stop()

    folder_name = st.text_input('Grand folder to save images', value='images_downloaded')

    if st.button('Start download'):
        with st.spinner("Downloading..."):
            try:
                os.makedirs(folder_name, exist_ok=True)

                results = []

                # Everything except the fixed metadata columns is treated as a link column
                fixed_cols = ["DATE", "Select City Name", "STORE NAME", "ID"]
                url_cols = [col for col in df.columns if col not in fixed_cols]

                # Only keep columns that actually contain http(s) links somewhere
                url_cols = [
                    col for col in url_cols
                    if df[col].astype(str).str.startswith(("http://", "https://")).any()
                ]

                future_to_row = {}
                # Track (date_folder, col_clean, dest_name) combos we've already
                # queued so duplicate ID/store/city rows don't silently overwrite
                # each other's files or get added to the zip twice.
                seen_targets = {}

                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    for _, row in df.iterrows():
                        date_folder_name = format_date_folder(row["DATE"])
                        city = sanitize_name(row["Select City Name"])
                        store = sanitize_store_name(row["STORE NAME"])
                        row_id = sanitize_name(row["ID"])

                        base_dest_name = f"{city}_{store}_{row_id}"

                        for col in url_cols:
                            url = str(row[col]).strip()
                            if not (url.startswith("http://") or url.startswith("https://")):
                                continue

                            col_clean = sanitize_name(col)
                            col_folder = os.path.join(folder_name, date_folder_name, col_clean)
                            os.makedirs(col_folder, exist_ok=True)

                            # Ensure a unique dest_name within this date/column folder
                            target_key = (date_folder_name, col_clean, base_dest_name)
                            seen_targets[target_key] = seen_targets.get(target_key, 0) + 1
                            if seen_targets[target_key] > 1:
                                dest_name = f"{base_dest_name}_{seen_targets[target_key]}"
                            else:
                                dest_name = base_dest_name

                            future = executor.submit(
                                download_one, username, password, url, dest_name, col_folder, timeout, max_retries
                            )
                            future_to_row[future] = (url, date_folder_name, col_clean, dest_name)

                    progress_bar = st.progress(0)
                    done = 0
                    total = len(future_to_row)
                    log_lines = []
                    log_placeholder = st.empty()

                    if total == 0:
                        st.warning("No valid links found to download.")

                    for future in as_completed(future_to_row):
                        url, date_folder_name, col_clean, dest_name = future_to_row[future]
                        success, final_name, error = future.result()
                        done += 1
                        if total:
                            progress_bar.progress(done / total)

                        if success:
                            log_lines.append(f'✅ {date_folder_name}/{col_clean}: {dest_name} -> {final_name}')
                            results.append((url, os.path.join(date_folder_name, col_clean, final_name), True, None))
                        else:
                            log_lines.append(f'❌ {date_folder_name}/{col_clean}: {url} -> {error}')
                            results.append((url, None, False, error))

                        if done % 10 == 0 or done == total:
                            log_placeholder.text("\n".join(log_lines[-20:]))

                succ = sum(1 for r in results if r[2])
                fail = sum(1 for r in results if not r[2])
                st.success(f"Download complete ✅ Successful: {succ}, Failed: {fail}")

                if succ > 0:
                    zip_buffer = BytesIO()
                    missing_files = []
                    added_arcnames = set()
                    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        for _, fname, ok, _ in results:
                            if ok and fname and fname not in added_arcnames:
                                fpath = os.path.join(folder_name, fname)
                                if os.path.exists(fpath):
                                    zipf.write(fpath, fname)
                                    added_arcnames.add(fname)
                                else:
                                    missing_files.append(fpath)
                    zip_buffer.seek(0)
                    st.download_button("Download ZIP", data=zip_buffer, file_name=f"{folder_name}.zip")
                    if missing_files:
                        st.warning(
                            f"{len(missing_files)} file(s) were marked successful but not found on disk "
                            f"when zipping. First few: {missing_files[:5]}"
                        )

                if fail > 0:
                    failed_links = [url for url, _, ok, _ in results if not ok]
                    fail_df = pd.DataFrame(failed_links, columns=['failed_url'])
                    csv_buffer = BytesIO()
                    fail_df.to_csv(csv_buffer, index=False)
                    st.download_button(
                        'Download failed links CSV',
                        data=csv_buffer.getvalue(),
                        file_name='failed_links.csv',
                        mime='text/csv'
                    )

            except Exception as e:
                st.error(f"Error: {e}")
else:
    st.info('Upload a file and enter your Kobo username & password to begin.')
