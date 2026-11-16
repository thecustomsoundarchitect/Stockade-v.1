/* -----------------------------------------------------------------------------
 *
 * Auto sync: BrickLink is the master.
 *
 * This file is an addition to BrickSync (altered source version, Stockade patches).
 * It is provided under the same terms as the rest of the software, see bricksync.c.
 *
 * One cycle, every autosyncinterval seconds:
 *  1. Check both stores for orders (order files on disk make each order count once).
 *  2. Read the live BrickLink inventory. A failed or empty read stops the cycle, nothing is written.
 *  3. Send the BrickOwl sales still owed to BrickLink, from the ledger file. Each one is recorded
 *     as "sent" before it leaves and is checked against the live BrickLink quantity afterwards,
 *     so a sale is deducted once even if a reply is lost or BrickSync is killed.
 *  4. The live BrickLink inventory becomes the saved copy. BrickLink is never overwritten with it.
 *  5. Read the live BrickOwl inventory and send only the differences.
 *
 * -----------------------------------------------------------------------------
 */

#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <math.h>
#include <time.h>

#include "cpuconfig.h"
#include "cc.h"
#include "ccstr.h"
#include "mm.h"
#include "mmatomic.h"
#include "mmbitmap.h"
#include "iolog.h"
#include "debugtrack.h"
#include "rand.h"

#if CC_UNIX
 #include <sys/types.h>
 #include <sys/stat.h>
 #include <unistd.h>
#elif CC_WINDOWS
 #include <windows.h>
 #include <direct.h>
#else
 #error Unknown/Unsupported platform!
#endif

#include "tcp.h"
#include "tcphttp.h"
#include "oauth.h"
#include "exclperm.h"
#include "journal.h"

#include "bsx.h"
#include "bsxpg.h"
#include "json.h"
#include "bsorder.h"
#include "bricklink.h"
#include "brickowl.h"
#include "colortable.h"
#include "bstranslation.h"

#include "bricksync.h"
#include "bsstop.h"
#include "bsevalgrade.h"


////


////


enum
{
  BS_AUTOSYNC_ENTRY_UNSENT,
  BS_AUTOSYNC_ENTRY_SENT
};

typedef struct
{
  /* BrickOwl order the deduction comes from */
  int64_t orderid;
  /* BrickLink lot to deduct from */
  int64_t lotid;
  /* Count to deduct, positive */
  int quantity;
  int state;
  /* SENT only: BrickLink quantity of the lot when the deduction was sent */
  int pre;
} bsAutoSyncEntry;

struct bsAutoSyncLedger
{
  int count;
  int alloc;
  bsAutoSyncEntry *list;
};

enum
{
  BS_AUTOSYNC_CYCLE_FAILED,
  BS_AUTOSYNC_CYCLE_SUCCESS,
  BS_AUTOSYNC_CYCLE_RESTART
};


////


static void bsAutoSyncSetError( bsContext *context, const char *format, ... )
{
  va_list ap;
  char *c;
  va_start( ap, format );
  vsnprintf( context->autosyncerror, sizeof(context->autosyncerror), format, ap );
  va_end( ap );
  /* Kept plain for the status file */
  for( c = context->autosyncerror ; *c ; c++ )
  {
    if( ( *c == '"' ) || ( *c == '\\' ) || ( (unsigned char)*c < 0x20 ) )
      *c = ' ';
  }
  ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: %s\n", context->autosyncerror );
  return;
}


static void bsAutoSyncLedgerAdd( struct bsAutoSyncLedger *ledger, int64_t orderid, int64_t lotid, int quantity, int state, int pre )
{
  bsAutoSyncEntry *entry;
  if( ledger->count >= ledger->alloc )
  {
    ledger->alloc = ( ledger->alloc ? ledger->alloc << 1 : 64 );
    ledger->list = realloc( ledger->list, ledger->alloc * sizeof(bsAutoSyncEntry) );
  }
  entry = &ledger->list[ ledger->count++ ];
  entry->orderid = orderid;
  entry->lotid = lotid;
  entry->quantity = quantity;
  entry->state = state;
  entry->pre = pre;
  return;
}


static void bsAutoSyncLedgerRemove( struct bsAutoSyncLedger *ledger, int index )
{
  memmove( &ledger->list[index], &ledger->list[index+1], ( ledger->count - index - 1 ) * sizeof(bsAutoSyncEntry) );
  ledger->count--;
  return;
}


int bsAutoSyncLedgerSave( bsContext *context, journalDef *journal )
{
  int index;
  ccGrowth growth;
  journalEntry journalentry;
  bsAutoSyncEntry *entry;
  struct bsAutoSyncLedger *ledger;

  DEBUG_SET_TRACKER();

  ledger = context->autosyncledger;
  ccGrowthInit( &growth, 4096 );
  ccGrowthPrintf( &growth, "BSLEDGER 1\n" );
  for( index = 0 ; index < ledger->count ; index++ )
  {
    entry = &ledger->list[index];
    ccGrowthPrintf( &growth, CC_LLD " " CC_LLD " %d %d %d\n", (long long)entry->orderid, (long long)entry->lotid, entry->quantity, entry->state, entry->pre );
  }
  if( !( ccFileStore( BS_AUTOSYNC_LEDGER_TEMP_FILE, growth.data, growth.offset, 1 ) ) )
  {
    ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_ERROR "Failed to write the pending BrickLink deductions as \"" IO_RED "%s" IO_WHITE "\".\n", BS_AUTOSYNC_LEDGER_TEMP_FILE );
    ccGrowthFree( &growth );
    return 0;
  }
  ccGrowthFree( &growth );
  if( journal )
    journalAddEntry( journal, BS_AUTOSYNC_LEDGER_TEMP_FILE, BS_AUTOSYNC_LEDGER_FILE, 0, 0 );
  else
  {
    journalentry.oldpath = BS_AUTOSYNC_LEDGER_TEMP_FILE;
    journalentry.newpath = BS_AUTOSYNC_LEDGER_FILE;
    if( !( journalExecute( BS_JOURNAL_FILE, BS_JOURNAL_TEMP_FILE, &context->output, &journalentry, 1 ) ) )
      return 0;
  }
  return 1;
}


static int bsAutoSyncLedgerLoad( bsContext *context )
{
  char *data, *line, *next;
  size_t datasize;
  long long orderid, lotid;
  int quantity, state, pre;
  struct bsAutoSyncLedger *ledger;

  ledger = context->autosyncledger;
  ledger->count = 0;
  if( !( ccFileExists( BS_AUTOSYNC_LEDGER_FILE ) ) )
    return 1;
  data = ccFileLoad( BS_AUTOSYNC_LEDGER_FILE, 64*1048576, &datasize );
  if( !( data ) )
    return 0;
  data = realloc( data, datasize + 1 );
  data[datasize] = 0;
  if( strncmp( data, "BSLEDGER 1\n", 11 ) )
  {
    free( data );
    return 0;
  }
  for( line = data + 11 ; *line ; line = next )
  {
    next = strchr( line, '\n' );
    if( next )
      *next++ = 0;
    else
      next = line + strlen( line );
    if( !( *line ) )
      continue;
    if( ( sscanf( line, "%lld %lld %d %d %d", &orderid, &lotid, &quantity, &state, &pre ) != 5 ) || ( quantity <= 0 ) || ( ( state != BS_AUTOSYNC_ENTRY_UNSENT ) && ( state != BS_AUTOSYNC_ENTRY_SENT ) ) )
    {
      free( data );
      ledger->count = 0;
      return 0;
    }
    bsAutoSyncLedgerAdd( ledger, orderid, lotid, quantity, state, pre );
  }
  free( data );
  return 1;
}


/* A BrickOwl order was merged into the tracked inventory with BS_MERGE_FLAGS_UPDATE_BRICKLINK:
   the BrickLink queue holds its deductions. Move them to the ledger, saved with the order's journal. */
int bsAutoSyncLedgerAddOrder( bsContext *context, int64_t orderid, journalDef *journal )
{
  int itemindex;
  bsxItem *item;
  bsxInventory *diffinv;

  DEBUG_SET_TRACKER();

  diffinv = context->bricklink.diffinv;
  for( itemindex = 0 ; itemindex < diffinv->itemcount ; itemindex++ )
  {
    item = &diffinv->itemlist[itemindex];
    if( item->flags & BSX_ITEM_FLAGS_DELETED )
      continue;
    if( item->quantity >= 0 )
      continue;
    if( item->lotid < 0 )
    {
      ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: BrickOwl order " CC_LLD " sold %d of \"%s\", which has no BrickLink lot to deduct from.\n", (long long)orderid, -item->quantity, ( item->id ? item->id : "?" ) );
      continue;
    }
    bsAutoSyncLedgerAdd( context->autosyncledger, orderid, item->lotid, -item->quantity, BS_AUTOSYNC_ENTRY_UNSENT, 0 );
    ioPrintf( &context->output, IO_MODEBIT_LOGONLY, "LOG: Auto sync: BrickOwl order " CC_LLD " owes BrickLink lot " CC_LLD " a deduction of %d.\n", (long long)orderid, (long long)item->lotid, -item->quantity );
  }
  bsxEmptyInventory( diffinv );
  return bsAutoSyncLedgerSave( context, journal );
}


/* A BrickLink order changed lots on BrickLink's side by the quantities of modinv (negative for a sale).
   A deduction already sent compares against the lot's quantity at send time: keep that in step. */
void bsAutoSyncLedgerBrickLinkOrder( bsContext *context, bsxInventory *modinv )
{
  int itemindex, index;
  bsxItem *item;
  bsAutoSyncEntry *entry;
  struct bsAutoSyncLedger *ledger;

  ledger = context->autosyncledger;
  for( itemindex = 0 ; itemindex < modinv->itemcount ; itemindex++ )
  {
    item = &modinv->itemlist[itemindex];
    if( ( item->flags & BSX_ITEM_FLAGS_DELETED ) || ( item->lotid < 0 ) )
      continue;
    for( index = 0 ; index < ledger->count ; index++ )
    {
      entry = &ledger->list[index];
      if( ( entry->state == BS_AUTOSYNC_ENTRY_SENT ) && ( entry->lotid == item->lotid ) )
        entry->pre += item->quantity;
    }
  }
  return;
}


////


static void bsAutoSyncWriteStatus( bsContext *context, int success )
{
  ccGrowth growth;
  ccGrowthInit( &growth, 1024 );
  ccGrowthPrintf( &growth, "{\n" );
  ccGrowthPrintf( &growth, "  \"cycle\": %d,\n", context->autosynccyclecount );
  ccGrowthPrintf( &growth, "  \"time\": " CC_LLD ",\n", (long long)time( 0 ) );
  ccGrowthPrintf( &growth, "  \"result\": \"%s\",\n", ( success ? "ok" : "failed" ) );
  ccGrowthPrintf( &growth, "  \"consecutive_failures\": %d,\n", context->autosyncfailcount );
  ccGrowthPrintf( &growth, "  \"alert\": %s,\n", ( context->autosyncfailcount >= BS_AUTOSYNC_ALERT_FAILCOUNT ? "true" : "false" ) );
  ccGrowthPrintf( &growth, "  \"error\": \"%s\",\n", ( success ? "" : context->autosyncerror ) );
  ccGrowthPrintf( &growth, "  \"pending_bricklink_deductions\": %d,\n", (context->autosyncledger)->count );
  ccGrowthPrintf( &growth, "  \"lots\": %d,\n", (context->inventory)->itemcount - (context->inventory)->itemfreecount );
  ccGrowthPrintf( &growth, "  \"next_cycle_time\": " CC_LLD "\n", (long long)context->autosynctime );
  ccGrowthPrintf( &growth, "}\n" );
  if( ccFileStore( BS_AUTOSYNC_STATUS_TEMP_FILE, growth.data, growth.offset, 0 ) )
    rename( BS_AUTOSYNC_STATUS_TEMP_FILE, BS_AUTOSYNC_STATUS_FILE );
  ccGrowthFree( &growth );
  return;
}


static int bsAutoSyncActiveLots( bsxInventory *inv )
{
  return inv->itemcount - inv->itemfreecount;
}


/* Send the deductions owed to BrickLink, against the live inventory "inv" just read.
   inv is updated to what BrickLink holds afterwards, less what is still owed.
   Returns the count of deductions that are still not confirmed, -1 on a file error. */
static int bsAutoSyncSettleLedger( bsContext *context, bsxInventory *inv )
{
  int index, result, sendquantity, pendingcount;
  bsxItem *item, *senditem;
  bsxInventory *sendinv;
  bsAutoSyncEntry *entry;
  struct bsAutoSyncLedger *ledger;

  DEBUG_SET_TRACKER();

  ledger = context->autosyncledger;
  pendingcount = 0;
  for( index = 0 ; index < ledger->count ; )
  {
    entry = &ledger->list[index];
    item = bsxFindLotID( inv, entry->lotid );

    if( entry->state == BS_AUTOSYNC_ENTRY_SENT )
    {
      if( !( item ) )
      {
        if( ( entry->pre - entry->quantity ) <= 0 )
          ioPrintf( &context->output, IO_MODEBIT_LOGONLY, "LOG: Auto sync: deduction of %d on BrickLink lot " CC_LLD " (BrickOwl order " CC_LLD ") is confirmed, the lot sold out.\n", entry->quantity, (long long)entry->lotid, (long long)entry->orderid );
        else
          ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: BrickLink lot " CC_LLD " is gone, the deduction of %d for BrickOwl order " CC_LLD " has nothing left to apply to.\n", (long long)entry->lotid, entry->quantity, (long long)entry->orderid );
        bsAutoSyncLedgerRemove( ledger, index );
        continue;
      }
      if( item->quantity == ( entry->pre - entry->quantity ) )
      {
        ioPrintf( &context->output, IO_MODEBIT_LOGONLY, "LOG: Auto sync: deduction of %d on BrickLink lot " CC_LLD " (BrickOwl order " CC_LLD ") is confirmed by the live quantity.\n", entry->quantity, (long long)entry->lotid, (long long)entry->orderid );
        bsAutoSyncLedgerRemove( ledger, index );
        continue;
      }
      if( item->quantity != entry->pre )
        ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: BrickLink lot " CC_LLD " holds %d, expected %d (deducted) or %d (not deducted) for BrickOwl order " CC_LLD ". The lot was edited meanwhile; deducting %d so it can not be oversold. Please review this lot.\n", (long long)entry->lotid, item->quantity, entry->pre - entry->quantity, entry->pre, (long long)entry->orderid, entry->quantity );
      /* Not applied (or unknown after an edit): send again below */
    }
    else if( !( item ) )
    {
      ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: BrickLink lot " CC_LLD " is gone, the deduction of %d for BrickOwl order " CC_LLD " has nothing left to apply to.\n", (long long)entry->lotid, entry->quantity, (long long)entry->orderid );
      bsAutoSyncLedgerRemove( ledger, index );
      continue;
    }

    sendquantity = entry->quantity;
    if( sendquantity > item->quantity )
    {
      ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: BrickOwl order " CC_LLD " sold %d of BrickLink lot " CC_LLD ", which only holds %d. Please review this lot.\n", (long long)entry->orderid, entry->quantity, (long long)entry->lotid, item->quantity );
      sendquantity = item->quantity;
    }
    if( sendquantity <= 0 )
    {
      bsAutoSyncLedgerRemove( ledger, index );
      continue;
    }

    /* Record it as sent, on disk, before it leaves */
    entry->quantity = sendquantity;
    entry->state = BS_AUTOSYNC_ENTRY_SENT;
    entry->pre = item->quantity;
    if( !( bsAutoSyncLedgerSave( context, 0 ) ) )
      return -1;

    /* Built from the live lot, so the note sent back with it is the one BrickLink holds now */
    sendinv = bsxNewInventory();
    senditem = bsxAddCopyItem( sendinv, item );
    senditem->quantity = -sendquantity;
    senditem->flags &= ~( BSX_ITEM_XFLAGS_TO_CREATE | BSX_ITEM_XFLAGS_TO_DELETE | BSX_ITEM_XFLAGS_TO_UPDATE | BSX_ITEM_XFLAGS_UPDATEMASK );
    if( ( item->quantity - sendquantity ) > 0 )
      senditem->flags |= BSX_ITEM_XFLAGS_TO_UPDATE | BSX_ITEM_XFLAGS_UPDATE_QUANTITY;
    else if( context->retainemptylotsflag )
      senditem->flags |= BSX_ITEM_XFLAGS_TO_UPDATE | BSX_ITEM_XFLAGS_UPDATE_QUANTITY | BSX_ITEM_XFLAGS_UPDATE_STOCKROOM;
    else
      senditem->flags |= BSX_ITEM_XFLAGS_TO_DELETE;
    result = bsBrickLinkApplySingle( context, senditem );
    bsxFreeInventory( sendinv );

    if( result == HTTP_RESULT_SUCCESS )
    {
      ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: deducted " IO_CYAN "%d" IO_DEFAULT " from BrickLink lot " IO_CYAN CC_LLD IO_DEFAULT " for BrickOwl order " IO_GREEN "#" CC_LLD IO_DEFAULT ".\n", sendquantity, (long long)entry->lotid, (long long)entry->orderid );
      bsxSetItemQuantity( inv, item, item->quantity - sendquantity );
      if( ( item->quantity <= 0 ) && !( context->retainemptylotsflag ) )
        bsxRemoveItem( inv, item );
      bsAutoSyncLedgerRemove( ledger, index );
      if( !( bsAutoSyncLedgerSave( context, 0 ) ) )
        return -1;
      continue;
    }

    /* Refused outright: it was not applied. Anything else: unknown, the next live read tells */
    if( ( result == HTTP_RESULT_CODE_ERROR ) || ( result == BS_HTTP_RESULT_RATELIMITED ) )
    {
      entry->state = BS_AUTOSYNC_ENTRY_UNSENT;
      entry->pre = 0;
      if( !( bsAutoSyncLedgerSave( context, 0 ) ) )
        return -1;
    }
    ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: the deduction of %d on BrickLink lot " CC_LLD " for BrickOwl order " CC_LLD " is not confirmed, it will be checked on the next cycle.\n", sendquantity, (long long)entry->lotid, (long long)entry->orderid );
    /* Until it is confirmed, the saved copy (and so BrickOwl) holds the lot less the sale */
    bsxSetItemQuantity( inv, item, item->quantity - sendquantity );
    pendingcount++;
    index++;
  }

  return pendingcount;
}


/* Fields BrickLink does not hold come from the previous saved copy, matched by BrickLink lot */
static void bsAutoSyncCarryOver( bsContext *context, bsxInventory *inv, bsxInventory *oldinv )
{
  int itemindex;
  bsxItem *item, *olditem;

  for( itemindex = 0 ; itemindex < inv->itemcount ; itemindex++ )
  {
    item = &inv->itemlist[itemindex];
    if( ( item->flags & BSX_ITEM_FLAGS_DELETED ) || ( item->lotid < 0 ) )
      continue;
    olditem = bsxFindLotID( oldinv, item->lotid );
    if( !( olditem ) )
      continue;
    if( ( olditem->typeid != item->typeid ) || ( olditem->colorid != item->colorid ) || ( olditem->condition != item->condition ) || !( ccStrCmpEqualTest( olditem->id, item->id ) ) )
      continue;
    if( item->boid == -1 )
      item->boid = olditem->boid;
    item->bolotid = olditem->bolotid;
    item->extid = olditem->extid;
    item->usedgrade = olditem->usedgrade;
  }
  return;
}


/* 1 if this BrickOwl update sets or clears a sale percentage, or creates a lot that needs one
   (BrickOwl takes no sale percentage on creation, it is set by the next comparison) */
static int bsAutoSyncDiffTouchesSale( bsxInventory *diffinv )
{
  int itemindex;
  bsxItem *item;

  for( itemindex = 0 ; itemindex < diffinv->itemcount ; itemindex++ )
  {
    item = &diffinv->itemlist[itemindex];
    if( item->flags & BSX_ITEM_FLAGS_DELETED )
      continue;
    if( ( item->flags & BSX_ITEM_XFLAGS_TO_UPDATE ) && ( item->flags & BSX_ITEM_XFLAGS_UPDATE_SALE ) )
      return 1;
    if( ( item->flags & BSX_ITEM_XFLAGS_TO_CREATE ) && ( item->sale > 0 ) )
      return 1;
  }
  return 0;
}


/* Used grades: a used item is Good, or Acceptable when its public note has one of the words picked by
   Tammy in it (bsevalgrade.c). New items and sets are left alone. The BrickOwl comparison then creates lots with this grade
   and corrects the grade of the lots BrickOwl already holds. */
static void bsAutoSyncSetUsedGrades( bsxInventory *inv )
{
  int itemindex;
  bsxItem *item;

  for( itemindex = 0 ; itemindex < inv->itemcount ; itemindex++ )
  {
    item = &inv->itemlist[itemindex];
    if( ( item->flags & BSX_ITEM_FLAGS_DELETED ) || ( item->condition != 'U' ) || ( item->typeid == 'S' ) )
      continue;
    item->usedgrade = ( bsNoteDescribesDefect( item->comments ) ? 'A' : 'G' );
  }
  return;
}


static int bsAutoSyncCycleOnce( bsContext *context )
{
  int pendingcount, owlpass, saleflag;
  int64_t ordertopdate;
  bsxInventory *inv;
  bsOrderList orderlist;
  bsSyncStats stats;
  journalDef journal;

  DEBUG_SET_TRACKER();

  /* 1. Orders first, on both stores */
  if( !( bsCheckBrickLink( context ) ) )
  {
    bsAutoSyncSetError( context, "the BrickLink order check failed" );
    return BS_AUTOSYNC_CYCLE_FAILED;
  }
  context->bricklink.lastchecktime = time( 0 );
  if( !( bsCheckBrickOwl( context ) ) )
  {
    bsAutoSyncSetError( context, "the BrickOwl order check failed" );
    return BS_AUTOSYNC_CYCLE_FAILED;
  }
  context->brickowl.lastchecktime = time( 0 );
  /* A BrickLink order queued relative BrickOwl updates: step 5 compares live instead */
  bsxEmptyInventory( context->brickowl.diffinv );
  bsxEmptyInventory( context->bricklink.diffinv );

  /* 2. Live BrickLink inventory, taken with no order unprocessed */
  if( !( inv = bsQueryBrickLinkFullState( context, &orderlist ) ) )
  {
    bsAutoSyncSetError( context, "reading the live BrickLink inventory failed, nothing was written" );
    return BS_AUTOSYNC_CYCLE_FAILED;
  }
  ordertopdate = orderlist.topdate;
  blFreeOrderList( &orderlist );
  if( ordertopdate >= context->bricklink.ordertopdate )
  {
    ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: a BrickLink order arrived during the inventory read, starting the cycle over.\n" );
    bsxFreeInventory( inv );
    return BS_AUTOSYNC_CYCLE_RESTART;
  }
  if( !( bsAutoSyncActiveLots( inv ) ) )
  {
    bsxFreeInventory( inv );
    bsAutoSyncSetError( context, "BrickLink returned an empty inventory (is the store closed?), nothing was written" );
    return BS_AUTOSYNC_CYCLE_FAILED;
  }
  bsInventoryFilterOutItems( context, inv );

  /* 3. BrickOwl sales owed to BrickLink */
  pendingcount = bsAutoSyncSettleLedger( context, inv );
  if( pendingcount < 0 )
  {
    bsxFreeInventory( inv );
    bsAutoSyncSetError( context, "the pending BrickLink deductions could not be saved to disk" );
    return BS_AUTOSYNC_CYCLE_FAILED;
  }
  bsxPackInventory( inv );

  /* 4. The live BrickLink inventory becomes the saved copy */
  bsAutoSyncCarryOver( context, inv, context->inventory );
  bsAutoSyncSetUsedGrades( inv );
  if( !( bsQueryBrickOwlLookupBoids( context, inv, BS_RESOLVE_FLAGS_TRYFALLBACK, 0 ) ) )
  {
    bsxFreeInventory( inv );
    bsAutoSyncSetError( context, "looking up BrickOwl item numbers for new BrickLink lots failed" );
    return BS_AUTOSYNC_CYCLE_FAILED;
  }
  bsxClampNegativeInventory( inv );
  bsxFreeInventory( context->inventory );
  context->inventory = inv;
  journalAlloc( &journal, 4 );
  if( !( bsSaveState( context, &journal ) ) )
    bsFatalError( context );
  if( !( bsxSaveInventory( BS_INVENTORY_TEMP_FILE, context->inventory, 1, 0 ) ) )
  {
    ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_ERROR "Failed to save inventory file \"" IO_RED "%s" IO_WHITE "\"!\n", BS_INVENTORY_TEMP_FILE );
    bsFatalError( context );
  }
  journalAddEntry( &journal, BS_INVENTORY_TEMP_FILE, BS_INVENTORY_FILE, 0, 0 );
  if( !( journalExecute( BS_JOURNAL_FILE, BS_JOURNAL_TEMP_FILE, &context->output, journal.entryarray, journal.entrycount ) ) )
    bsFatalError( context );
  journalFree( &journal );
  context->contextflags &= ~BS_CONTEXT_FLAGS_UPDATED_INVENTORY;
  context->bricklink.lastsynctime = time( 0 );

  /* 5. BrickOwl: live read, then only the differences.
     If a reply was lost on the way, read BrickOwl again right away and settle what is left */
  for( owlpass = 0 ; ; owlpass++ )
  {
    context->stateflags &= ~( BS_STATE_FLAGS_BRICKOWL_MUST_CHECK | BS_STATE_FLAGS_BRICKOWL_MUST_SYNC );
    if( !( bsSyncBrickOwl( context, &stats ) ) )
    {
      if( context->stateflags & BS_STATE_FLAGS_BRICKOWL_MUST_CHECK )
      {
        ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: a BrickOwl order arrived during the inventory read, starting the cycle over.\n" );
        return BS_AUTOSYNC_CYCLE_RESTART;
      }
      bsAutoSyncSetError( context, "reading the live BrickOwl inventory failed, BrickOwl was not updated" );
      return BS_AUTOSYNC_CYCLE_FAILED;
    }
    saleflag = bsAutoSyncDiffTouchesSale( context->brickowl.diffinv );
    if( !( bsQueryBrickOwlApplyDiff( context, context->brickowl.diffinv, 0 ) ) )
    {
      bsxEmptyInventory( context->brickowl.diffinv );
      bsAutoSyncSetError( context, "the BrickOwl update did not complete, the next cycle compares again and finishes it" );
      return BS_AUTOSYNC_CYCLE_FAILED;
    }
    bsxEmptyInventory( context->brickowl.diffinv );
    if( !( context->stateflags & BS_STATE_FLAGS_BRICKOWL_MUST_SYNC ) && !( saleflag ) )
      break;
    if( owlpass >= 2 )
    {
      if( !( context->stateflags & BS_STATE_FLAGS_BRICKOWL_MUST_SYNC ) )
      {
        /* A sale percentage BrickOwl does not show is not a failed sync */
        ioPrintf( &context->output, IO_MODEBIT_LOGONLY, "LOG: Auto sync: BrickOwl does not show a sale percentage that was sent.\n" );
        break;
      }
      bsAutoSyncSetError( context, "BrickOwl replies kept getting lost, the next cycle compares again" );
      return BS_AUTOSYNC_CYCLE_FAILED;
    }
    if( context->stateflags & BS_STATE_FLAGS_BRICKOWL_MUST_SYNC )
      ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: a BrickOwl reply was lost, reading BrickOwl again to confirm.\n" );
    else
      ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: a sale percentage was sent, reading BrickOwl back to confirm it.\n" );
  }
  context->brickowl.lastsynctime = time( 0 );

  if( pendingcount )
  {
    bsAutoSyncSetError( context, "%d BrickOwl sale deduction(s) are not confirmed on BrickLink yet", pendingcount );
    return BS_AUTOSYNC_CYCLE_FAILED;
  }
  return BS_AUTOSYNC_CYCLE_SUCCESS;
}


#define BS_AUTOSYNC_ALL_WORK_FLAGS ( BS_STATE_FLAGS_BRICKLINK_MUST_CHECK | BS_STATE_FLAGS_BRICKOWL_MUST_CHECK | BS_STATE_FLAGS_BRICKLINK_MUST_UPDATE | BS_STATE_FLAGS_BRICKOWL_MUST_UPDATE | BS_STATE_FLAGS_BRICKLINK_MUST_SYNC | BS_STATE_FLAGS_BRICKOWL_MUST_SYNC | BS_STATE_FLAGS_BRICKLINK_PARTIAL_SYNC | BS_STATE_FLAGS_BRICKOWL_PARTIAL_SYNC )

void bsAutoSyncTick( bsContext *context )
{
  int attempt, result;

  DEBUG_SET_TRACKER();

  /* The check and sync commands (and SIGUSR1) ask for a cycle now */
  if( !( context->stateflags & BS_AUTOSYNC_ALL_WORK_FLAGS ) && ( context->curtime < context->autosynctime ) )
    return;

  ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: starting a cycle.\n" );
  context->autosyncerror[0] = 0;
  result = BS_AUTOSYNC_CYCLE_FAILED;
  for( attempt = 0 ; attempt < 3 ; attempt++ )
  {
    result = bsAutoSyncCycleOnce( context );
    if( ( result != BS_AUTOSYNC_CYCLE_RESTART ) || ( bsStopRequested() ) )
      break;
  }
  if( result == BS_AUTOSYNC_CYCLE_RESTART )
  {
    bsAutoSyncSetError( context, "orders kept arriving during the inventory reads" );
    result = BS_AUTOSYNC_CYCLE_FAILED;
  }

  /* Nothing is left queued: every cycle starts from live reads */
  context->stateflags &= ~BS_AUTOSYNC_ALL_WORK_FLAGS;
  bsxEmptyInventory( context->bricklink.diffinv );
  bsxEmptyInventory( context->brickowl.diffinv );

  context->curtime = time( 0 );
  context->autosynctime = context->curtime + context->autosyncinterval;
  context->bricklink.checktime = context->autosynctime;
  context->brickowl.checktime = context->autosynctime;

  /* A stop request makes the operation in progress fail on purpose: that is not a failed cycle */
  if( !( ( result == BS_AUTOSYNC_CYCLE_FAILED ) && ( bsStopRequested() ) ) )
  {
    context->autosynccyclecount++;
    if( result == BS_AUTOSYNC_CYCLE_SUCCESS )
    {
      if( context->autosyncfailcount )
        ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: back to normal after %d failed cycle(s).\n", context->autosyncfailcount );
      context->autosyncfailcount = 0;
      context->autosyncalertsent = 0;
      ioPrintf( &context->output, 0, BSMSG_INFO "Auto sync: cycle complete, BrickOwl matches BrickLink (" IO_CYAN "%d" IO_DEFAULT " lots).\n", bsAutoSyncActiveLots( context->inventory ) );
    }
    else
    {
      /* One count per cycle, whatever the attempts inside it */
      context->autosyncfailcount++;
      ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Auto sync: cycle failed (" IO_RED "%d" IO_WHITE " in a row). Next cycle in %d seconds.\n", context->autosyncfailcount, context->autosyncinterval );
      if( ( context->autosyncfailcount >= BS_AUTOSYNC_ALERT_FAILCOUNT ) && !( context->autosyncalertsent ) )
      {
        ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_ERROR "Auto sync: ALERT, %d sync cycles failed in a row: %s\n", context->autosyncfailcount, context->autosyncerror );
        context->autosyncalertsent = 1;
      }
    }
    bsAutoSyncWriteStatus( context, ( result == BS_AUTOSYNC_CYCLE_SUCCESS ) );
  }

  if( !( bsSaveState( context, 0 ) ) )
    bsFatalError( context );
  return;
}


void bsAutoSyncInit( bsContext *context )
{
  DEBUG_SET_TRACKER();

  context->autosyncledger = malloc( sizeof(struct bsAutoSyncLedger) );
  memset( context->autosyncledger, 0, sizeof(struct bsAutoSyncLedger) );
  context->autosynctime = 0;
  context->autosyncfailcount = 0;
  context->autosynccyclecount = 0;
  context->autosyncalertsent = 0;
  context->autosyncerror[0] = 0;
  if( !( context->autosyncflag ) )
    return;
#ifndef BS_TEST_HOOKS
  if( context->autosyncinterval < BS_AUTOSYNC_INTERVAL_MIN )
    context->autosyncinterval = BS_AUTOSYNC_INTERVAL_MIN;
#endif
  if( !( bsAutoSyncLedgerLoad( context ) ) )
  {
    ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_ERROR "The file of pending BrickLink deductions \"" IO_RED "%s" IO_WHITE "\" is damaged. Fix or remove it, then start again.\n", BS_AUTOSYNC_LEDGER_FILE );
    bsFatalError( context );
  }
  ioPrintf( &context->output, 0, BSMSG_INIT "Auto sync is " IO_GREEN "on" IO_DEFAULT ": BrickLink is the master, a cycle runs every " IO_CYAN "%d" IO_DEFAULT " seconds. Pending BrickLink deductions: " IO_CYAN "%d" IO_DEFAULT ".\n", context->autosyncinterval, (context->autosyncledger)->count );
  if( ( context->stateflags & ( BS_STATE_FLAGS_BRICKLINK_MUST_UPDATE | BS_STATE_FLAGS_BRICKLINK_MUST_SYNC ) ) && !( ccFileExists( BS_AUTOSYNC_LEDGER_FILE ) ) )
    ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "The last run without auto sync left BrickLink updates unsent. They are not replayed: please check recent BrickOwl orders against BrickLink once.\n" );
  return;
}
