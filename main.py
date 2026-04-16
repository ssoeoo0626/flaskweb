import io
import re
from typing import List, Dict, Tuple, Optional

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="PDF 키워드 기반 표 추출기", layout="wide")

# -----------------------------
# 유틸
# -----------------------------

def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def keyword_hit_score(text: str, keywords: List[str]) -> int:
    lowered = text.lower()
    return sum(1 for kw in keywords if kw.lower() in lowered)


def clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.fillna("")
    df.columns = [str(c).strip() if c is not None else "" for c in df.columns]
    df = df.applymap(lambda x: normalize_text(str(x)) if x is not None else "")

    # 완전히 빈 행 제거
    mask = df.apply(lambda row: any(str(v).strip() for v in row), axis=1)
    df = df.loc[mask].reset_index(drop=True)
    return df


def promote_first_row_to_header(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    first_row = [str(x).strip() for x in df.iloc[0].tolist()]
    non_empty_ratio = sum(bool(x) for x in first_row) / max(len(first_row), 1)
    if non_empty_ratio >= 0.5:
        new_cols = []
        seen = {}
        for i, col in enumerate(first_row):
            col = col if col else f"col_{i+1}"
            seen[col] = seen.get(col, 0) + 1
            if seen[col] > 1:
                col = f"{col}_{seen[col]}"
            new_cols.append(col)
        out = df.iloc[1:].reset_index(drop=True).copy()
        out.columns = new_cols
        return out
    return df


def dataframe_to_tsv(df: pd.DataFrame) -> str:
    return df.to_csv(sep="\t", index=False)


# -----------------------------
# PDF 분석
# -----------------------------

def extract_page_text(page) -> str:
    text = page.extract_text() or ""
    return normalize_text(text)


def extract_tables_from_page(page) -> List[pd.DataFrame]:
    tables = []
    settings_list = [
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 5,
            "snap_tolerance": 3,
            "join_tolerance": 3,
        },
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "intersection_tolerance": 5,
            "text_x_tolerance": 2,
            "text_y_tolerance": 2,
        },
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "text",
            "intersection_tolerance": 5,
            "text_x_tolerance": 2,
            "text_y_tolerance": 2,
        },
    ]

    for settings in settings_list:
        try:
            raw_tables = page.extract_tables(settings)
            for raw in raw_tables:
                try:
                    df = pd.DataFrame(raw)
                    df = clean_table(df)
                    if not df.empty and len(df.columns) >= 2 and len(df) >= 2:
                        tables.append(df)
                except Exception:
                    continue
        except Exception:
            continue
    return tables


def find_candidate_pages(pdf_file, keywords: List[str]) -> List[Dict]:
    results = []
    with pdfplumber.open(pdf_file) as pdf:
        for idx, page in enumerate(pdf.pages):
            text = extract_page_text(page)
            score = keyword_hit_score(text, keywords)
            if score > 0:
                snippet = text[:500]
                results.append(
                    {
                        "page_number": idx + 1,
                        "score": score,
                        "snippet": snippet,
                    }
                )
    results.sort(key=lambda x: (-x["score"], x["page_number"]))
    return results


def extract_best_table_from_page(pdf_file, page_number: int, keywords: List[str]) -> Tuple[Optional[pd.DataFrame], List[pd.DataFrame]]:
    with pdfplumber.open(pdf_file) as pdf:
        page = pdf.pages[page_number - 1]
        tables = extract_tables_from_page(page)
        if not tables:
            return None, []

        scored = []
        for df in tables:
            text_blob = "\n".join(
                [" ".join(map(str, df.columns.tolist()))] +
                [" ".join(map(str, row)) for row in df.astype(str).values.tolist()]
            )
            score = keyword_hit_score(text_blob, keywords)
            scored.append((score, df))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_df = scored[0][1]
        return best_df, [x[1] for x in scored]


def try_merge_tables_vertically(tables: List[pd.DataFrame]) -> pd.DataFrame:
    if not tables:
        return pd.DataFrame()

    normalized = []
    for df in tables:
        temp = promote_first_row_to_header(df)
        normalized.append(temp)

    base_cols = list(normalized[0].columns)
    aligned = []
    for df in normalized:
        if len(df.columns) == len(base_cols):
            df = df.copy()
            df.columns = base_cols
            aligned.append(df)
        else:
            aligned.append(df)

    try:
        merged = pd.concat(aligned, ignore_index=True)
        merged = clean_table(merged)
        return merged
    except Exception:
        return normalized[0]


# -----------------------------
# UI
# -----------------------------

st.title("PDF 키워드 기반 표 추출기")
st.caption("PDF를 올리고 키워드를 넣으면 관련 페이지를 찾고, 표를 뽑아 TSV로 복사할 수 있습니다.")

with st.sidebar:
    st.header("설정")
    keyword_input = st.text_area(
        "키워드 입력",
        value="revenue\ngoodwill\ninstallations",
        help="줄바꿈으로 여러 키워드를 넣어주세요. 예: revenue / EBITDA / screen / installations"
    )
    promote_header = st.checkbox("첫 행을 헤더로 승격", value=True)
    allow_multi_merge = st.checkbox("후보 표 여러 개 세로 병합 시도", value=False)

uploaded_file = st.file_uploader("PDF 업로드", type=["pdf"])

if uploaded_file:
    keywords = [k.strip() for k in keyword_input.splitlines() if k.strip()]

    if not keywords:
        st.warning("키워드를 1개 이상 넣어주세요.")
    else:
        pdf_bytes = uploaded_file.read()

        with st.spinner("PDF를 분석 중입니다..."):
            candidate_pages = find_candidate_pages(io.BytesIO(pdf_bytes), keywords)

        if not candidate_pages:
            st.error("입력한 키워드가 포함된 페이지를 찾지 못했습니다.")
        else:
            st.subheader("키워드가 잡힌 페이지")
            candidate_df = pd.DataFrame(candidate_pages)
            st.dataframe(candidate_df, use_container_width=True)

            page_options = [f"p.{row['page_number']} | score={row['score']}" for _, row in candidate_df.iterrows()]
            selected_label = st.selectbox("분석할 페이지 선택", page_options)
            selected_page = int(re.search(r"p\.(\d+)", selected_label).group(1))

            with st.spinner("표를 추출 중입니다..."):
                best_df, all_tables = extract_best_table_from_page(io.BytesIO(pdf_bytes), selected_page, keywords)

            if best_df is None:
                st.warning("이 페이지에서 추출 가능한 표를 찾지 못했습니다. PDF가 이미지형이면 OCR 기반 보강이 필요합니다.")
            else:
                if allow_multi_merge and len(all_tables) >= 2:
                    result_df = try_merge_tables_vertically(all_tables)
                else:
                    result_df = best_df.copy()

                if promote_header:
                    result_df = promote_first_row_to_header(result_df)

                result_df = clean_table(result_df)

                st.subheader("추출 결과")
                st.dataframe(result_df, use_container_width=True)

                tsv_text = dataframe_to_tsv(result_df)
                st.subheader("엑셀 붙여넣기용 TSV")
                st.text_area("아래 내용을 복사해서 Excel에 붙여넣으세요", value=tsv_text, height=250)

                csv_bytes = result_df.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    label="CSV 다운로드",
                    data=csv_bytes,
                    file_name=f"extracted_table_p{selected_page}.csv",
                    mime="text/csv"
                )

                st.download_button(
                    label="TSV 다운로드",
                    data=tsv_text.encode("utf-8-sig"),
                    file_name=f"extracted_table_p{selected_page}.tsv",
                    mime="text/tab-separated-values"
                )

st.divider()
st.markdown(
    """
### 이 앱이 잘하는 것
- 텍스트형 PDF에서 키워드가 포함된 페이지 찾기
- 해당 페이지의 표 자동 추출
- 엑셀 붙여넣기용 TSV 변환

### 한계
- 스캔본 PDF(이미지형)는 기본 `pdfplumber`만으로는 추출이 잘 안 될 수 있음
- 표 선이 없거나 깨진 PDF는 후처리가 더 필요할 수 있음

### 다음 단계 추천
1. OCR 추가 (`pytesseract` 또는 `ocrmypdf`)
2. 여러 페이지 연속 표 병합
3. 표 유형별 자동 정규화 (예: 손익계산서 / 운영관수 / 국가별 표)
4. 사용자가 원하는 출력 포맷 preset 추가
"""
)

# 실행 예시
# streamlit run app.py
