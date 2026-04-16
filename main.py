import io
import re
from collections import Counter
from typing import List, Dict, Tuple, Optional

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="PDF 추출기 + 어닝콜 분석", layout="wide")


# =========================================================
# 공통 유틸
# =========================================================
def normalize_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def dataframe_to_tsv(df: pd.DataFrame) -> str:
    return df.to_csv(sep="\t", index=False)


# =========================================================
# PDF 표 추출 유틸
# =========================================================
def keyword_hit_score(text: str, keywords: List[str]) -> int:
    lowered = text.lower()
    return sum(1 for kw in keywords if kw.lower() in lowered)


def clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.fillna("")
    df.columns = [str(c).strip() if c is not None else "" for c in df.columns]
    df = df.map(lambda x: normalize_text(str(x)) if x is not None else "")

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
                results.append(
                    {
                        "page_number": idx + 1,
                        "score": score,
                        "snippet": text[:500],
                    }
                )

    results.sort(key=lambda x: (-x["score"], x["page_number"]))
    return results


def parse_text_table_from_page(page, keywords: List[str]) -> Optional[pd.DataFrame]:
    text = extract_page_text(page)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    records = []
    current_section = ""

    for line in lines:
        lower = line.lower()

        if lower in [
            "assets",
            "liabilities and stockholders' equity",
            "liabilities and stockholders’ equity",
            "revenue",
            "revenue:",
            "operating activities",
            "operating activities:",
            "investing activities",
            "investing activities:",
            "financing activities",
            "financing activities:",
        ]:
            current_section = line.rstrip(":")
            continue

        if line.endswith(":"):
            current_section = line[:-1]
            continue

        match = re.match(r"^(.*?)(\(?\$?[\d,]+\)?)[ ]+(\(?\$?[\d,]+\)?)$", line)
        if match:
            account = normalize_text(match.group(1))
            value_1 = normalize_text(match.group(2)).replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
            value_2 = normalize_text(match.group(3)).replace("$", "").replace(",", "").replace("(", "-").replace(")", "")

            records.append(
                {
                    "section": current_section,
                    "account": account,
                    "value_1": value_1,
                    "value_2": value_2,
                }
            )

    if not records:
        return None

    df = pd.DataFrame(records)
    df = clean_table(df)

    text_blob = "\n".join(df.astype(str).fillna("").agg(" ".join, axis=1).tolist())
    if keyword_hit_score(text_blob, keywords) == 0:
        return None

    return df


def extract_best_table_from_page(
    pdf_file, page_number: int, keywords: List[str]
) -> Tuple[Optional[pd.DataFrame], List[pd.DataFrame]]:
    with pdfplumber.open(pdf_file) as pdf:
        page = pdf.pages[page_number - 1]

        tables = extract_tables_from_page(page)

        scored = []
        for df in tables:
            text_blob = "\n".join(
                [" ".join(map(str, df.columns.tolist()))]
                + [" ".join(map(str, row)) for row in df.astype(str).values.tolist()]
            )
            score = keyword_hit_score(text_blob, keywords)
            scored.append((score, df))

        scored.sort(key=lambda x: x[0], reverse=True)

        if scored and scored[0][0] > 0:
            return scored[0][1], [x[1] for x in scored]

        fallback_df = parse_text_table_from_page(page, keywords)
        if fallback_df is not None:
            return fallback_df, [fallback_df]

        return None, []


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
            copied = df.copy()
            copied.columns = base_cols
            aligned.append(copied)
        else:
            aligned.append(df)

    try:
        merged = pd.concat(aligned, ignore_index=True)
        return clean_table(merged)
    except Exception:
        return normalized[0]


def filter_by_sector(result_df: pd.DataFrame, sector_keywords: List[str]) -> Dict[str, pd.DataFrame]:
    output = {}

    if result_df.empty:
        return output

    if "section" in result_df.columns:
        search_series = (
            result_df.get("section", "").astype(str).fillna("")
            + " "
            + result_df.get("account", "").astype(str).fillna("")
        )
    else:
        first_col = result_df.columns[0]
        search_series = result_df[first_col].astype(str).fillna("")

    for sector in sector_keywords:
        mask = search_series.str.contains(sector, case=False, na=False)
        output[sector] = result_df.loc[mask].copy()

    return output


# =========================================================
# 어닝콜 분석 유틸
# =========================================================
DEFAULT_STOPWORDS = {
    "the", "and", "of", "to", "in", "a", "for", "on", "is", "that", "with", "as",
    "we", "our", "it", "this", "be", "are", "was", "were", "by", "from", "at",
    "have", "has", "had", "will", "would", "can", "could", "should", "may",
    "an", "or", "not", "but", "if", "so", "than", "then", "into", "about",
    "you", "your", "they", "their", "them", "he", "she", "his", "her",
    "operator", "question", "questions", "answer", "thanks", "thank",
    "quarter", "year", "years", "good", "morning", "afternoon", "evening",
    "hello", "please", "okay", "yeah", "uh", "um", "really", "think",
    "company", "business"
}


def extract_text_from_uploaded_doc(uploaded_file) -> str:
    if uploaded_file is None:
        return ""

    file_name = uploaded_file.name.lower()
    file_bytes = uploaded_file.read()

    if file_name.endswith(".txt"):
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return file_bytes.decode("cp949", errors="ignore")

    if file_name.endswith(".pdf"):
        texts = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
        return "\n".join(texts)

    return ""


def tokenize_english_text(text: str) -> List[str]:
    text = text.lower()
    tokens = re.findall(r"[a-zA-Z][a-zA-Z\-']{1,}", text)
    return tokens


def build_stopwords(custom_stopwords_text: str) -> set:
    custom_words = {w.strip().lower() for w in custom_stopwords_text.splitlines() if w.strip()}
    return DEFAULT_STOPWORDS | custom_words


def get_top_keywords(tokens: List[str], stopwords: set, min_len: int = 3, top_n: int = 20) -> pd.DataFrame:
    filtered = [t for t in tokens if len(t) >= min_len and t not in stopwords]
    counter = Counter(filtered)
    return pd.DataFrame(counter.most_common(top_n), columns=["keyword", "count"])


def get_top_bigrams(tokens: List[str], stopwords: set, min_len: int = 3, top_n: int = 20) -> pd.DataFrame:
    filtered = [t for t in tokens if len(t) >= min_len and t not in stopwords]
    bigrams = [" ".join([filtered[i], filtered[i + 1]]) for i in range(len(filtered) - 1)]
    counter = Counter(bigrams)
    return pd.DataFrame(counter.most_common(top_n), columns=["bigram", "count"])


def build_kwic(text: str, query: str, window: int = 80, limit: int = 20) -> pd.DataFrame:
    if not query.strip():
        return pd.DataFrame(columns=["context"])

    matches = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for m in pattern.finditer(text):
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)
        context = text[start:end].replace("\n", " ")
        matches.append({"context": context})
        if len(matches) >= limit:
            break

    return pd.DataFrame(matches)


def split_paragraphs(text: str) -> List[str]:
    parts = re.split(r"\n\s*\n", text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts


def summarize_paragraph(paragraph: str, stopwords: set, max_sentences: int = 2) -> str:
    sentences = re.split(r"(?<=[\.\?\!])\s+", paragraph.replace("\n", " "))
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return ""

    tokens = tokenize_english_text(paragraph)
    freq = Counter([t for t in tokens if t not in stopwords and len(t) >= 3])

    scored = []
    for sent in sentences:
        sent_tokens = tokenize_english_text(sent)
        score = sum(freq.get(tok, 0) for tok in sent_tokens)
        scored.append((score, sent))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_sentences = [s for _, s in scored[:max_sentences]]

    ordered = [s for s in sentences if s in top_sentences]
    return " ".join(ordered)


def build_paragraph_summary_df(text: str, stopwords: set, max_paragraphs: int = 30) -> pd.DataFrame:
    paragraphs = split_paragraphs(text)

    rows = []
    for i, para in enumerate(paragraphs[:max_paragraphs], start=1):
        summary = summarize_paragraph(para, stopwords, max_sentences=2)
        token_count = len(tokenize_english_text(para))

        rows.append(
            {
                "paragraph_no": i,
                "summary": summary,
                "original_text": para,
                "token_count": token_count,
            }
        )

    return pd.DataFrame(rows)


# =========================================================
# 세션 상태
# =========================================================
if "show_sector_box" not in st.session_state:
    st.session_state.show_sector_box = False


# =========================================================
# 탭 구성
# =========================================================
tab_pdf, tab_earnings = st.tabs(["PDF 표 추출", "어닝콜 키워드 분석"])


# =========================================================
# 탭 1: PDF 표 추출
# =========================================================
with tab_pdf:
    st.title("PDF 키워드 기반 표 추출기")
    st.caption("PDF를 올리고 키워드를 넣으면 관련 페이지를 찾고, 표를 뽑아 TSV로 복사할 수 있습니다.")

    with st.sidebar:
        st.header("PDF 추출 설정")

        keyword_input = st.text_area(
            "키워드 입력",
            value="""balance sheets
assets
total assets
retained earnings""",
            help="줄바꿈으로 여러 키워드를 넣어주세요. 예: balance sheets / revenue / operating activities",
            key="pdf_keyword_input",
        )

        promote_header = st.checkbox("첫 행을 헤더로 승격", value=True, key="pdf_promote_header")
        allow_multi_merge = st.checkbox("후보 표 여러 개 세로 병합 시도", value=False, key="pdf_allow_merge")

        st.divider()

        if st.button("섹터 설정", key="pdf_sector_button"):
            st.session_state.show_sector_box = not st.session_state.show_sector_box

        sector_keywords = []
        if st.session_state.show_sector_box:
            sector_mode = st.selectbox(
                "섹터 프리셋",
                ["직접 입력", "재무제표", "손익계산서", "현금흐름표"],
                key="pdf_sector_mode",
            )

            preset_map = {
                "재무제표": ["assets", "liabilities", "equity"],
                "손익계산서": ["revenue", "gross profit", "operating income", "net income"],
                "현금흐름표": ["operating activities", "investing activities", "financing activities"],
            }

            default_sector_text = ""
            if sector_mode in preset_map:
                default_sector_text = "\n".join(preset_map[sector_mode])

            sector_text = st.text_area(
                "섹터 입력",
                value=default_sector_text,
                help="줄바꿈으로 섹터 키워드를 입력하세요.",
                key="pdf_sector_text",
            )
            sector_keywords = [x.strip() for x in sector_text.splitlines() if x.strip()]

    uploaded_file = st.file_uploader("PDF 업로드", type=["pdf"], key="pdf_uploader")

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

                page_options = [
                    f"p.{row['page_number']} | score={row['score']}"
                    for _, row in candidate_df.iterrows()
                ]
                selected_label = st.selectbox("분석할 페이지 선택", page_options, key="pdf_page_select")
                selected_page = int(re.search(r"p\.(\d+)", selected_label).group(1))

                with st.spinner("표를 추출 중입니다..."):
                    best_df, all_tables = extract_best_table_from_page(
                        io.BytesIO(pdf_bytes),
                        selected_page,
                        keywords,
                    )

                if best_df is None:
                    st.warning("이 페이지에서 표 선 기반 추출은 실패했고, 텍스트 파싱으로도 적절한 표를 만들지 못했습니다.")
                else:
                    if allow_multi_merge and len(all_tables) >= 2:
                        result_df = try_merge_tables_vertically(all_tables)
                    else:
                        result_df = best_df.copy()

                    if promote_header:
                        result_df = promote_first_row_to_header(result_df)

                    result_df = clean_table(result_df)

                    st.subheader("추출 결과")

                    if sector_keywords:
                        sector_map = filter_by_sector(result_df, sector_keywords)
                        tab_names = ["전체"] + sector_keywords
                        result_tabs = st.tabs(tab_names)

                        with result_tabs[0]:
                            st.dataframe(result_df, use_container_width=True)

                        for i, sector in enumerate(sector_keywords, start=1):
                            with result_tabs[i]:
                                sector_df = sector_map.get(sector, pd.DataFrame())
                                if sector_df.empty:
                                    st.info(f"'{sector}' 키워드로 분류된 행이 없습니다.")
                                else:
                                    st.dataframe(sector_df, use_container_width=True)
                    else:
                        st.dataframe(result_df, use_container_width=True)

                    tsv_text = dataframe_to_tsv(result_df)
                    st.subheader("엑셀 붙여넣기용 TSV")
                    st.code(tsv_text, language=None)

                    csv_bytes = result_df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        label="CSV 다운로드",
                        data=csv_bytes,
                        file_name=f"extracted_table_p{selected_page}.csv",
                        mime="text/csv",
                        key="pdf_csv_download",
                    )


# =========================================================
# 탭 2: 어닝콜 키워드 분석 + 문단별 요약
# =========================================================
with tab_earnings:
    st.title("어닝콜 키워드 분석")
    st.caption("텍스트를 붙여넣거나 transcript 파일을 올리면 키워드/구문/문단별 요약을 볼 수 있습니다.")

    col1, col2 = st.columns([1, 1])

    with col1:
        transcript_text = st.text_area(
            "어닝콜 텍스트 붙여넣기",
            height=320,
            placeholder="여기에 earnings call transcript를 붙여넣으세요.",
            key="earnings_text_input",
        )

    with col2:
        transcript_file = st.file_uploader(
            "또는 transcript 파일 업로드 (txt/pdf)",
            type=["txt", "pdf"],
            key="earnings_file_uploader",
        )

        custom_stopwords_text = st.text_area(
            "추가 불용어",
            value="""operator
question
answer
quarter
year
company""",
            help="줄바꿈으로 추가 불용어를 입력하세요.",
            key="earnings_stopwords",
        )

        min_word_len = st.slider("최소 단어 길이", 2, 8, 3, key="earnings_min_word_len")
        top_n = st.slider("Top N", 10, 50, 20, key="earnings_top_n")
        max_paragraphs = st.slider("문단 요약 개수", 5, 50, 20, key="earnings_max_paragraphs")

    file_text = extract_text_from_uploaded_doc(transcript_file) if transcript_file else ""
    full_text = transcript_text.strip() if transcript_text.strip() else file_text.strip()

    if full_text:
        stopwords = build_stopwords(custom_stopwords_text)
        tokens = tokenize_english_text(full_text)

        keyword_df = get_top_keywords(tokens, stopwords, min_len=min_word_len, top_n=top_n)
        bigram_df = get_top_bigrams(tokens, stopwords, min_len=min_word_len, top_n=top_n)

        kpi1, kpi2, kpi3 = st.columns(3)
        kpi1.metric("총 토큰 수", f"{len(tokens):,}")
        kpi2.metric("고유 토큰 수", f"{len(set(tokens)):,}")
        kpi3.metric("문단 수", f"{len(split_paragraphs(full_text)):,}")

        st.divider()

        left, right = st.columns(2)

        with left:
            st.subheader("Top Keywords")
            if keyword_df.empty:
                st.info("추출된 키워드가 없습니다.")
            else:
                st.bar_chart(keyword_df.set_index("keyword"))
                st.dataframe(keyword_df, use_container_width=True)

        with right:
            st.subheader("Top Bigrams")
            if bigram_df.empty:
                st.info("추출된 bigram이 없습니다.")
            else:
                st.bar_chart(bigram_df.set_index("bigram"))
                st.dataframe(bigram_df, use_container_width=True)

        st.divider()

        st.subheader("문단별 요약")
        paragraph_summary_df = build_paragraph_summary_df(
            full_text,
            stopwords,
            max_paragraphs=max_paragraphs
        )

        if paragraph_summary_df.empty:
            st.info("문단 요약을 만들지 못했습니다.")
        else:
            for row in paragraph_summary_df.itertuples(index=False):
                with st.expander(f"문단 {row.paragraph_no} | 토큰 {row.token_count}개"):
                    st.markdown("요약")
                    st.write(row.summary if row.summary else "(요약 생성 실패)")
                    st.markdown("원문")
                    st.write(row.original_text)

        st.divider()

        st.subheader("복사용 Top Keywords")
        if not keyword_df.empty:
            keyword_lines = "\n".join(
                [f"{row.keyword}\t{row.count}" for row in keyword_df.itertuples(index=False)]
            )
            st.code(keyword_lines, language=None)

        st.subheader("원문 검색")
        search_query = st.text_input("찾을 단어/문구", key="earnings_search_query")

        if search_query.strip():
            kwic_df = build_kwic(full_text, search_query, window=100, limit=30)
            if kwic_df.empty:
                st.info("검색 결과가 없습니다.")
            else:
                st.dataframe(kwic_df, use_container_width=True)

        with st.expander("원문 전체 보기"):
            st.text(full_text[:50000])

    else:
        st.info("텍스트를 붙여넣거나 txt/pdf transcript 파일을 업로드하세요.")
