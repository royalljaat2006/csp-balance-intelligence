"""
Synthetic bank statements in the exact layout observed in the real SBI
pool-account export (tab-delimited text in a `.xls` file). The real sample
contains customer names and account numbers, so it is deliberately NOT copied
into the repo -- these fixtures reproduce its structure with invented data.
"""

from __future__ import annotations

import decimal

D = decimal.Decimal

HEADER_TEMPLATE = (
    "Account Number               :\t{account}\n"
    "Description                  :\tCA-GEN-PUB OTH-NONRURAL-INR\n"
    "Name                         :\tEKO BHARAT VENTURES PRIVATE LIMITED                         \n"
    "Currency                     :\tINR\n"
    "Corporate Address            :\t547, EXAMPLE ENCLAVE\n"
    "                              \t\n"
    "                              \tNEW DELHI\n"
    "Branch                       :\tSWASTHYA VIHAR(07085)\n"
    "Rate of Interest (% p.a.)    :\t0.0% \n"
    "IFS Code                     :\tSBIN0007085\n"
    "Book Balance                 :\t{book}\n"
    "Available Balance            :\t{available}\n"
    "Hold Value                   :\t{hold}\n"
    "MOD Balance             :\t0.00\n"
    "Uncleared Amount             :\t0.00\n"
    "Opening Balance as on {start}        :\t{opening_fmt}\n"
    "Start Date                   :\t{start}\n"
    "End Date                     :\t{start}\n"
    "Txn Date\tValue Date\tDescription\tRef No./Cheque No.\tBranch Code\t"
    "        Debit\tCredit\tBalance\t\n"
)

POOL_ACCOUNT = "_00000034757671556"


def desc_withdrawal(ref: str, ko: str, acct: str = "36471609123") -> str:
    return (
        f"BY TRANSFER-{ref}AEPSWDL TXN@KO{ko}-TRANSFER FROM {acct}"
        "                         Mr. Test  Customer-"
    )


def desc_deposit(ref: str, ko: str, acct: str = "30707699536") -> str:
    return (
        f"TO TRANSFER-{ref}AEPSDeposit TXN@KO{ko}-TRANSFER TO {acct}"
        "                           Mrs. Test  Customer-"
    )


def desc_moneytrf(ref: str, ko: str) -> str:
    return f"TO TRANSFER-{ref} MoneyTRF TXN@KO {ko}-TRANSFER TO 20089891630   Mr. Someone-"


def desc_offus(ref: str, ko: str) -> str:
    return f"BY TRANSFER-{ref}RupayOFFUSWDL@KO{ko}-TRANSFER FROM 4599142105216-"


def indian(amount: D) -> str:
    """57,68,774.00 style grouping, as in the real header."""
    s = f"{amount:.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return f"{whole}.{frac}"


def make_statement(
    rows: list[tuple[str, str, str]],
    *,
    opening: str = "1000.00",
    book: str | None = None,
    hold: str = "0.00",
    available: str | None = None,
    account: str = POOL_ACCOUNT,
    start: str = "12 Oct 2026",
    footer: bool = True,
    txn_date: str = "12/10/2026",
    corrupt_balance_at: int | None = None,
    end: str | None = None,
) -> str:
    """rows: (description, debit, credit[, dd/mm/yyyy date]) -- amounts as strings ('' for none);
    an optional 4th element overrides the row's date (for multi-day statements)."""
    balance = D(opening)
    body = []
    for i, row in enumerate(rows):
        desc, debit, credit = row[0], row[1], row[2]
        row_date = row[3] if len(row) > 3 else txn_date
        balance = balance - (D(debit) if debit else D(0)) + (D(credit) if credit else D(0))
        shown = balance + (D("1.00") if corrupt_balance_at == i else D(0))
        body.append(
            f"{row_date}	{row_date}	{desc}	TRANSFER ref	10521	"
            f"{debit or ' '}	{credit or ' '}	{shown:.2f}	"
        )
    book_val = D(book) if book is not None else balance
    avail_val = D(available) if available is not None else book_val - D(hold)
    text = HEADER_TEMPLATE.format(
        account=account,
        book=f"{book_val:.2f}",
        available=f"{avail_val:.2f}",
        hold=hold,
        start=start,
        end=end or start,
        opening_fmt=indian(D(opening)),
    )
    text += "\n".join(body) + "\n"
    if footer:
        text += "\n**This is a computer generated statement and does not require a signature\n"
    return text


def sample_rows(n_extra: int = 0) -> list[tuple[str, str, str]]:
    rows = [
        (desc_withdrawal("62850709168200095256", "1A852455"), "", "4000.00"),
        (desc_moneytrf("62850711251200095256", "1A852455"), "3100.00", ""),
        (desc_deposit("62850715239900018296", "1A850295"), "10000.00", ""),
        (desc_withdrawal("62850720372300012013", "1A8502957"), "", "1000.00"),  # trailing variant
        (desc_withdrawal("62850721128400095257", "1A85094"), "", "500.00"),  # truncated
        (desc_withdrawal("62850721128400095258", "1A859999"), "", "250.00"),  # unmatched
        (desc_offus("62850754192500062149", "1A851803"), "", "100.00"),
        ("CSH DEP (CDM)-7696642763                            3470--", "", "49500.00"),
        (desc_withdrawal("62850730147200089273", "1A852455"), "", "3000.00"),
    ]
    for i in range(n_extra):
        rows.append((desc_withdrawal(f"6285090000000009{i:04d}", "1A852455"), "", "100.00"))
    return rows


def resave_like_spreadsheet(text: str) -> str:
    """Reproduce the differences seen in a real statement that was opened and re-saved
    from a spreadsheet: quoted opening balance, 2-digit-year dates in the header,
    trailing tab padding, amounts without trailing zeros, collapsed column padding."""
    import re

    out = []
    for line in text.splitlines():
        if line.startswith("Opening Balance"):
            label, value = line.split(":\t", 1)
            line = f'{label}:\t"{value}"'
        elif line.startswith(("Start Date", "End Date")):
            label, value = line.split(":\t", 1)
            day, mon, year = value.split()
            line = f"{label}:\t{int(day):02d}-{mon}-{year[2:]}"
        elif re.match(r"\d\d/\d\d/\d{4}\t", line):
            cells = line.split("\t")
            cells[2] = re.sub(r" {2,}", " ", cells[2])
            for k in (5, 6, 7):
                cells[k] = re.sub(r"\.00$", "", cells[k].strip())
            line = "\t".join(cells[:8]) + "\t\t\t"
        out.append(line)
    return "\n".join(out) + "\n"
