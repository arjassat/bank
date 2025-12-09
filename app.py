import streamlit as st
import pandas as pd
import re
from io import BytesIO
import os
import pdfplumber

# --- 2. HELPER FUNCTIONS ---
def clean_value(value):
    """
    Cleans numeric values by handling SA (comma for decimal, space/dot for thousands) format.
    Kept as a safety net for the extraction output.
    """
    if not isinstance(value, str):
        # Handle direct float/int from extraction output
        if isinstance(value, (int, float)):
            return float(value)
        return None
    
    value = str(value).strip().replace('\n', '').replace('\r', '')
    
    # 1. Remove currency symbols and merge spaces between digits
    value = re.sub(r'[R$]', '', value, flags=re.IGNORECASE)
    value = re.sub(r'(\d)\s+(\d)', r'\1\2', value)
    # 2. Handle South African formatting (1 000,00 or 1.000,00)
    if ',' in value:
        value = value.replace('.', '').replace(' ', '')
    else: # Handle standard dot-as-decimal format if no comma is present
        value = value.replace(' ', '')
    # 3. Replace the South African decimal comma with a standard dot
    value = value.replace(',', '.')
    
    # 4. Clean up formatting indicators (Dr/Cr)
    # NOTE: This ensures that if the extraction missed the sign, the 'Dr' prefix/suffix is converted to a minus sign.
    value = value.replace('Cr', '').replace('Dr', '-').strip()
    
    # 5. Final aggressive cleanup to remove non-numeric/non-dot/non-sign characters
    value = re.sub(r'[^\d\.\-]+', '', value)
    
    try:
        if re.match(r'^-?\d*\.?\d+$', value):
            return float(value)
        return None
    except:
        return None

def clean_description_for_xero(description):
    """Cleans up transaction descriptions for easy Xero reconciliation."""
    if not isinstance(description, str): return ""
    
    description = description.strip()
    
    # Remove common reference/date patterns left over by extraction
    description = re.sub(r'\s*\d{6}\s+\d{4}\s+\d{2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', '', description, flags=re.IGNORECASE)
    description = re.sub(r'(?:Ref\s*|Reference\s*|No\s*|Nr\s*|ID\s*):\s*[\w\d\-]+', '', description, flags=re.IGNORECASE)
    description = re.sub(r'Serial:\d+/\d+', '', description)
    # Remove common transaction type prefixes
    description = re.sub(r'(?:POS Purchase|ATM Withdrawal|Immediate Payment|Internet Pmt To|Teller Transfer Debit|Direct Credit|EFT|IB Payment)\s*', '', description, flags=re.IGNORECASE)
    
    description = re.sub(r'\s{2,}', ' ', description).strip(' -').strip()
    
    return description

# --- 4. CORE EXTRACTION LOGIC (PDFPLUMBER ONLY - WITH PRECISION COLUMN EXCLUSION) ---
def extract_from_pdf(pdf_file_path: BytesIO, file_name: str) -> tuple[pd.DataFrame, str | None]:
    """
    PRIMARY METHOD: Uses pdfplumber for extraction based on table detection,
    focusing on excluding the dedicated Fees (R) column, extracting the StatementYear,
    and enforcing the sign convention (Credit = Positive, Debit = Negative).
    Returns a DataFrame and the extracted year (as a string, or None on failure).
    """
    st.info("🔄 **Initiating PDF Extraction...** (Extracting Year and Transactions)")
    try:
        with pdfplumber.open(pdf_file_path) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            
            # Extract statement year from text (adjust regex based on common patterns in SA bank statements)
            year_pattern = r'(?:Statement Period|Statement Date).*?(\d{4})'
            match = re.search(year_pattern, full_text, re.IGNORECASE)
            statement_year = match.group(1) if match else None
            
            # Extract tables from all pages
            all_tables = []
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if table and len(table) > 1:  # Ensure there's a header and data
                        # Create DataFrame, stripping whitespace from headers
                        df_table = pd.DataFrame(table[1:], columns=[col.strip() if col else '' for col in table[0]])
                        all_tables.append(df_table)
            
            if not all_tables:
                st.error(f"No tables detected in {file_name}. Ensure the PDF contains selectable text/tables.")
                return pd.DataFrame(), None
            
            # Concatenate all tables
            df = pd.concat(all_tables, ignore_index=True)
            
            # Standardize column names (handle variations in bank statement layouts)
            column_mapping = {
                'Date': ['Date', 'Trans Date', 'Transaction Date'],
                'Description': ['Description', 'Details', 'Transaction Details'],
                'Debits': ['Debits (R)', 'Debits', 'Debit (R)', 'Debit Amount'],
                'Credits': ['Credits (R)', 'Credits', 'Credit (R)', 'Credit Amount'],
                'Fees': ['Fees (R)', 'Fees', 'Service Fee'],
                'Amount': ['Amount', 'Value', 'Transaction Amount'],  # If single amount column
                'Balance': ['Balance (R)', 'Balance', 'Running Balance']
            }
            
            for standard, possibles in column_mapping.items():
                for poss in possibles:
                    if poss in df.columns:
                        df.rename(columns={poss: standard}, inplace=True)
                        break
            
            # Exclude the Fees column if present
            if 'Fees' in df.columns:
                df = df.drop('Fees', axis=1)
            
            # Handle Amount: Combine Debits/Credits if separate, or clean single Amount
            if 'Amount' not in df.columns:
                df['Amount'] = 0.0
                if 'Credits' in df.columns:
                    df['Amount'] += df['Credits'].apply(clean_value).fillna(0)
                if 'Debits' in df.columns:
                    df['Amount'] -= df['Debits'].apply(clean_value).fillna(0)
            else:
                # If single Amount column, apply cleaning (handles Dr/Cr signs)
                df['Amount'] = df['Amount'].apply(clean_value)
            
            # Drop unnecessary columns
            drop_cols = [col for col in ['Debits', 'Credits', 'Balance', 'Fees'] if col in df.columns]
            df = df.drop(columns=drop_cols, errors='ignore')
            
            # Filter valid transaction rows (non-empty Date, Description, non-zero Amount)
            df = df[(df['Date'].notna() & (df['Date'].str.strip() != '')) &
                    (df['Description'].notna() & (df['Description'].str.strip() != '')) &
                    (df['Amount'].notna() & (df['Amount'] != 0))]
            
            if df.empty:
                st.error(f"No valid transactions extracted from {file_name} after filtering.")
                return pd.DataFrame(), None
            
            st.success(f"Extraction successful! Year **{statement_year or 'Not Found'}** extracted with {len(df)} transactions.")
            return df[['Date', 'Description', 'Amount']], statement_year
        
    except Exception as e:
        st.error(f"PDF extraction failed for {file_name} due to an unexpected error. Error: {e}")
        return pd.DataFrame(), None

def parse_pdf_data(pdf_file_path, file_name):
    """Core function: Uses pdfplumber for extraction, returning DataFrame and Year."""
    
    pdf_file_path.seek(0)
    
    # Capture both the DataFrame and the extracted year
    df_transactions, statement_year = extract_from_pdf(pdf_file_path, file_name)
    
    if not df_transactions.empty and 'Amount' in df_transactions.columns:
        required_cols = ['Date', 'Description', 'Amount']
        if not all(col in df_transactions.columns for col in required_cols):
            st.error("Extraction output is missing required columns (Date, Description, Amount).")
            return pd.DataFrame(), None
        df_transactions['Date'] = df_transactions['Date'].astype(str)
        df_transactions['Description'] = df_transactions['Description'].astype(str)
        
        # Use clean_value to standardize the numbers and convert any lingering 'Dr' to '-'
        df_transactions['Amount'] = df_transactions['Amount'].apply(lambda x: clean_value(x))
        df_transactions.dropna(subset=['Amount'], inplace=True)
        
        if not df_transactions.empty:
            # Return the processed DataFrame and the extracted year
            return df_transactions[['Date', 'Description', 'Amount']], statement_year
    st.error(f"Extraction failed for {file_name}. No data or year extracted.")
    return pd.DataFrame(), None

# --- 5. STREAMLIT APP LOGIC ---
if 'uploaded_files' not in st.session_state:
    st.session_state['uploaded_files'] = []

st.set_page_config(page_title="🇿🇦 Free SA Bank Statement to CSV Converter (pdfplumber)", layout="wide")
st.title("🇿🇦 SA Bank Statement PDF to CSV Converter (Free, No API Key)")
st.markdown("""
    ### Using **pdfplumber** (a free, open-source library) to extract the statement year and transactions, filtering out the dedicated Fees (R) column. **Credit/Debit sign is enforced** (Credit=Positive, Debit=Negative).
    ---
""")

uploaded_files = st.file_uploader(
    "Upload your bank statement PDF files (Multiple files supported)",
    type=["pdf"],
    accept_multiple_files=True,
    key="unique_pdf_uploader_fixed"
)

# --- PROCESSING STARTS HERE ---
if uploaded_files:
    st.subheader("Processing Files...")
    
    all_df = []
    
    for uploaded_file in uploaded_files:
        file_name = uploaded_file.name
        st.markdown(f"**Processing:** `{file_name}`")
        
        pdf_data = BytesIO(uploaded_file.read())
        
        # Capture both the DataFrame and the dynamically extracted year
        df_transactions, statement_year = parse_pdf_data(pdf_data, file_name)
        if not df_transactions.empty and 'Amount' in df_transactions.columns and statement_year:
            
            # The dynamically extracted year is now used for standardization
            current_year = statement_year
            
            # Apply final cleaning and formatting
            df_transactions['Description'] = df_transactions['Description'].apply(clean_description_for_xero)
            
            df_final = df_transactions.rename(columns={
                'Date': 'Date',
                'Description': 'Description',
                'Amount': 'Amount'
            })
            
            # --- START: DATE FIX IMPLEMENTATION (Using dynamic year) ---
            try:
                # 1. Clean the date string
                df_final['Date_Raw'] = df_final['Date'].astype(str).str.strip()
                # 2. Append the correct year to the extracted date (e.g., '01 Sep' -> '01 Sep 2025')
                df_final['Date_With_Year'] = df_final['Date_Raw'] + ' ' + current_year
                # 3. Attempt to parse the date using the explicit 'Day AbbreviatedMonth Year' format, which is common.
                df_final['Date_Parsed'] = pd.to_datetime(
                    df_final['Date_With_Year'],
                    format='%d %b %Y',
                    errors='coerce'
                )
                # 4. Handle cases where the extraction may have output the date in a standard format or failed step 3
                failed_parsing = df_final['Date_Parsed'].isna()
                if failed_parsing.any():
                    # Fallback to general dayfirst parsing on the original raw date
                    df_final.loc[failed_parsing, 'Date_Parsed'] = pd.to_datetime(
                        df_final.loc[failed_parsing, 'Date_Raw'],
                        errors='coerce',
                        dayfirst=True
                    )
                
                # 5. Format and update the final 'Date' column
                df_final['Date'] = df_final['Date_Parsed'].dt.strftime('%d/%m/%Y')
                
                # Drop rows where date parsing still failed
                df_final.dropna(subset=['Date'], inplace=True)
                
            except Exception as e:
                st.warning(f"Could not standardize dates for {file_name}. Dates remain in raw format. Error: {e}")
            # --- END: DATE FIX IMPLEMENTATION ---
            
            # Final structure: Date, Description, Amount
            df_xero = pd.DataFrame({
                'Date': df_final['Date'].fillna(''),
                'Description': df_final['Description'].astype(str),
                'Amount': df_final['Amount'].round(2),
            })
            
            # Ensure the order is exactly Date, Description, Amount
            df_xero = df_xero[['Date', 'Description', 'Amount']]
            
            df_xero.dropna(subset=['Date', 'Amount'], inplace=True)
            
            all_df.append(df_xero)
            
            st.success(f"Successfully extracted {len(df_xero)} transactions from {file_name} (Year: {statement_year})")
    
    # --- 6. COMBINE AND DOWNLOAD ---
    if all_df:
        final_combined_df = pd.concat(all_df, ignore_index=True)
        
        st.markdown("---")
        st.subheader("✅ All Transactions Combined and Ready for Download (Fees Column Excluded, Year Dynamic)")
        
        st.dataframe(final_combined_df)
        
        # Convert DataFrame to CSV for download
        csv_output = final_combined_df.to_csv(index=False, sep=',', encoding='utf-8')
        st.download_button(
            label="⬇️ Download Column-Filtered CSV File",
            data=csv_output,
            file_name="SA_Bank_Statements_Dynamic_Year_Export.csv",
            mime="text/csv"
        )
