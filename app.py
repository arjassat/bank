import streamlit as st
import pandas as pd
import re
from io import BytesIO
import os
from pdf2image import convert_from_bytes
import pytesseract
from PIL import Image

# --- 2. HELPER FUNCTIONS ---
def clean_value(value):
    """
    Cleans numeric values by handling SA (comma for decimal, space/dot for thousands) format.
    Kept as a safety net for the AI's JSON output.
    """
    if not isinstance(value, str):
        # Handle direct float/int from AI's JSON output
        if isinstance(value, (int, float)):
            return float(value)
        return None
    
    value = str(value).strip().replace('\n', '').replace('\r', '')
    
    # 1. Remove currency symbols and merge spaces between digits
    value = re.sub(r'[R$]', '', value, flags=re.IGNORECASE)
    value = re.sub(r'(\d)\s+(\d)', r'\1\2', value)
    # 2. Handle South African formatting (1 000,00 or 1.000,00)
    if ',' in value and '.' in value:
        # Assume dot thousand, comma decimal
        value = value.replace('.', '').replace(',', '.')
    elif ',' in value:
        value = value.replace(',', '.')
    value = value.replace(' ', '')
    
    # 4. Clean up formatting indicators (Dr/Cr)
    # NOTE: This ensures that if the AI missed the sign, the 'Dr' prefix/suffix is converted to a minus sign.
    if 'dr' in value.lower():
        value = '-' + re.sub(r'[^\d\.]', '', value)
    elif 'cr' in value.lower():
        value = re.sub(r'[^\d\.]', '', value)
    else:
        value = re.sub(r'[^\d\.\-]+', '', value)
    
    try:
        return float(value)
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

# --- 4. CORE EXTRACTION LOGIC (OCR with pytesseract - FOR IMAGE-BASED PDFs) ---
def extract_from_pdf(pdf_file_path: BytesIO, file_name: str) -> tuple[pd.DataFrame, str | None]:
    """
    Uses OCR (pytesseract) to extract text from image-based PDFs, then parses for year and transactions.
    Focuses on excluding fees if detected, extracting StatementYear, and enforcing sign convention.
    Returns a DataFrame and the extracted year (as a string, or None on failure).
    """
    st.info("🔄 **Initiating OCR Extraction...** (Extracting Year and Transactions from Image-based PDF)")
    try:
        # Convert PDF to images
        images = convert_from_bytes(pdf_file_path.getvalue())
        
        full_text = ''
        for image in images:
            text = pytesseract.image_to_string(image)
            full_text += text + '\n\n'
        
        if not full_text.strip():
            st.error(f"No text extracted from {file_name}. Ensure the PDF has readable content.")
            return pd.DataFrame(), None
        
        # Extract statement year
        year_pattern = r'(?:Statement Period|Statement Date).*?(\d{4})'
        match = re.search(year_pattern, full_text, re.IGNORECASE)
        statement_year = match.group(1) if match else None
        
        # Parse transactions from text
        lines = full_text.splitlines()
        transactions = []
        in_table = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Detect table header to start parsing
            if re.search(r'Date.*Description.*Amount.*Balance.*Accrued', line, re.IGNORECASE):
                in_table = True
                continue
            if in_table:
                # Improved regex to capture date, desc, amount, balance, charges
                match = re.match(r'(\d{1,2} \w{3})\s+(.*)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2} ?(?:Cr|Dr)?)\s+([\d.]+)$', line)
                if match:
                    date = match.group(1)
                    desc = match.group(2).strip()
                    amt_str = match.group(3).replace(',', '')
                    balance_str = match.group(4)
                    charges = match.group(5)
                    try:
                        amt = float(amt_str)
                    except ValueError:
                        continue
                    
                    # Determine sign based on description keywords
                    desc_lower = desc.lower()
                    credit_keywords = ['from', 'credit', 'deposit', 'rtc', 'geo payment from', 'credit absa']
                    if any(keyword in desc_lower for keyword in credit_keywords):
                        amt = abs(amt)
                    else:
                        amt = -abs(amt)
                    
                    transactions.append({'Date': date, 'Description': desc, 'Amount': amt})
                # If line doesn't match, perhaps end of table
                elif re.search(r'total|balance|summary|closing|turnover', line, re.IGNORECASE):
                    in_table = False
        
        if not transactions:
            st.error(f"No transactions parsed from {file_name}. Adjust parsing logic if format differs.")
            return pd.DataFrame(), None
        
        df = pd.DataFrame(transactions)
        
        # Exclude fees: Filter out rows where description indicates fee (customize as needed)
        df = df[~df['Description'].str.contains('fee|charge|service', case=False, na=False)]
        
        st.success(f"OCR Extraction successful! Year **{statement_year or 'Not Found'}** extracted with {len(df)} transactions.")
        return df[['Date', 'Description', 'Amount']], statement_year
    
    except Exception as e:
        st.error(f"OCR Extraction failed for {file_name} due to an unexpected error. Error: {e}")
        return pd.DataFrame(), None

def parse_pdf_data(pdf_file_path, file_name):
    """Core function: Uses OCR for extraction, returning DataFrame and Year."""
    
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
        df_transactions['Amount'] = df_transactions['Amount'].apply(lambda x: clean_value(str(x)))
        df_transactions.dropna(subset=['Amount'], inplace=True)
        
        if not df_transactions.empty:
            # Return the processed DataFrame and the extracted year
            return df_transactions[['Date', 'Description', 'Amount']], statement_year
    st.error(f"Extraction failed for {file_name}. No data or year extracted.")
    return pd.DataFrame(), None

# --- 5. STREAMLIT APP LOGIC ---
if 'uploaded_files' not in st.session_state:
    st.session_state['uploaded_files'] = []

st.set_page_config(page_title="🇿🇦 Free SA Bank Statement to CSV Converter (OCR)", layout="wide")
st.title("🇿🇦 SA Bank Statement PDF to CSV Converter (Free OCR, No API Key)")
st.markdown("""
    ### Using **pytesseract** (free, open-source OCR) to handle image-based PDFs, extract year and transactions, filtering fees. **Credit/Debit sign enforced**.
    ---
""")

st.sidebar.success("OCR Engine: **Active** ✅ (Handles scanned/image PDFs - No API Key Required)")

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
        st.subheader("✅ All Transactions Combined and Ready for Download (Fees Excluded, Year Dynamic)")
        
        st.dataframe(final_combined_df)
        
        # Convert DataFrame to CSV for download
        csv_output = final_combined_df.to_csv(index=False, sep=',', encoding='utf-8')
        st.download_button(
            label="⬇️ Download Column-Filtered CSV File",
            data=csv_output,
            file_name="SA_Bank_Statements_Dynamic_Year_Export.csv",
            mime="text/csv"
        )
