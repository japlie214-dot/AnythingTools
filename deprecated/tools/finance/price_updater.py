# deprecated/tools/finance/price_updater.py
from datetime import datetime, timedelta
import yfinance
import pandas as pd
from database.writer import enqueue_write
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

async def update_stock_prices(ticker: str) -> tuple[bool, bool]:
    '''
    Returns (was_updated, was_full_replace).
    Appends incremental data if DB has existing prices,
    or fetches full history on first call.
    '''
    ticker = ticker.upper()
    conn = DatabaseManager.get_read_connection()
    row = conn.execute(
        'SELECT MAX(date) FROM stock_prices WHERE ticker=?', (ticker,)
    ).fetchone()
    latest_db_date = row[0] if row and row[0] else None

    yf_ticker = yfinance.Ticker(ticker)

    if not latest_db_date:
        # Full history fetch
        hist = yf_ticker.history(period='max', auto_adjust=True)
        full_replace = True
    else:
        # Incremental: fetch from 7 days before latest to catch corrections
        start = (datetime.strptime(latest_db_date, '%Y-%m-%d')
                 - timedelta(days=7)).strftime('%Y-%m-%d')
        hist = yf_ticker.history(start=start, auto_adjust=True)
        full_replace = False

    if hist.empty:
        return False, False

    hist.reset_index(inplace=True)
    hist.rename(columns={'Date':'date','Close':'close',
                         'Open':'open','High':'high',
                         'Low':'low','Volume':'volume'}, inplace=True)
    hist['date'] = pd.to_datetime(hist['date']).dt.strftime('%Y-%m-%d')
    hist['ticker'] = ticker

    if latest_db_date:
        hist = hist[hist['date'] > latest_db_date]

    # Collect all records into a list for bulk insert
    records = [
        (ticker, row['date'], row.get('open'), row.get('high'),
         row.get('low'), row.get('close'), row.get('volume'))
        for _, row in hist.iterrows()
    ]

    # Check if enqueue_write_many exists, otherwise use bulk transaction
    if records:
        try:
            from database.writer import enqueue_write_many
            enqueue_write_many(
                '''INSERT OR REPLACE INTO stock_prices
                   (ticker, date, open, high, low, close, volume)
                   VALUES (?,?,?,?,?,?,?)''',
                records
            )
        except (ImportError, AttributeError):
            # Fallback: use single transaction with executemany
            from database.connection import DatabaseManager
            conn = DatabaseManager.get_write_connection()
            try:
                conn.executemany(
                    '''INSERT OR REPLACE INTO stock_prices
                       (ticker, date, open, high, low, close, volume)
                       VALUES (?,?,?,?,?,?,?)''',
                    records
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise e
            finally:
                conn.close()

        log.dual_log(
            tag="Finance:PriceUpdater",
            message=f'Price update: {len(records)} rows for {ticker}.',
            level="INFO",
            payload={'rows': len(records), 'ticker': ticker},
        )
        return True, full_replace
    
    return False, full_replace
