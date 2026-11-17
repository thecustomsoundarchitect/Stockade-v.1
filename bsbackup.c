/* Stockade daily local record backups. Altered source addition, under BrickSync's licence. */

#if defined(__APPLE__)

#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <errno.h>
#include <fcntl.h>
#include <dirent.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "cpuconfig.h"
#include "cc.h"
#include "ccstr.h"
#include "mm.h"
#include "mmatomic.h"
#include "mmbitmap.h"
#include "iolog.h"
#include "rand.h"
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

extern char **environ;


static void bsDailyBackupError( bsContext *context, const char *action, const char *path )
{
  ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Daily backup: could not %s \"%s\": %s.\n", action, path, strerror( errno ) );
}


static int bsDailyBackupSyncDirectory( bsContext *context )
{
  int fd, result;
  fd = open( BS_BACKUP_DIR, O_RDONLY | O_DIRECTORY );
  if( fd < 0 )
  {
    bsDailyBackupError( context, "open backup directory for saving", BS_BACKUP_DIR );
    return 0;
  }
  result = 1;
  if( fsync( fd ) )
  {
    bsDailyBackupError( context, "save backup directory", BS_BACKUP_DIR );
    result = 0;
  }
  if( close( fd ) )
  {
    bsDailyBackupError( context, "close saved backup directory", BS_BACKUP_DIR );
    result = 0;
  }
  return result;
}


static time_t bsDailyBackupTime( void )
{
  time_t now;
  now = time( 0 );
#ifdef BS_TEST_HOOKS
  {
    FILE *file;
    long long value;
    file = fopen( BS_GLOBAL_PATH "test.dailybackup-time", "r" );
    if( file )
    {
      if( fscanf( file, "%lld", &value ) == 1 )
        now = (time_t)value;
      fclose( file );
    }
  }
#endif
  return now;
}


static int bsDailyBackupDate( const char *name )
{
  int index, year, month, day;
  static const int days[] = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 };
  for( index = 0 ; index < 10 ; index++ )
  {
    if( ( index == 4 ) || ( index == 7 ) )
    {
      if( name[index] != '-' )
        return 0;
    }
    else if( ( name[index] < '0' ) || ( name[index] > '9' ) )
      return 0;
  }
  year = ( name[0] - '0' ) * 1000 + ( name[1] - '0' ) * 100 + ( name[2] - '0' ) * 10 + name[3] - '0';
  month = ( name[5] - '0' ) * 10 + name[6] - '0';
  day = ( name[8] - '0' ) * 10 + name[9] - '0';
  if( ( year < 1 ) || ( month < 1 ) || ( month > 12 ) || ( day < 1 ) )
    return 0;
  return day <= days[month-1] + ( ( month == 2 ) && !( year % 4 ) && ( ( year % 100 ) || !( year % 400 ) ) );
}


static int bsDailyBackupSnapshotName( const char *name )
{
  int index;
  if( ( strlen( name ) != 9 ) || strcmp( name + 5, ".bsx" ) )
    return 0;
  for( index = 0 ; index < 5 ; index++ )
  {
    if( ( name[index] < '0' ) || ( name[index] > '9' ) )
      return 0;
  }
  return 1;
}


static int bsDailyBackupPruneDirectory( bsContext *context, const char *path, time_t cutoff )
{
  DIR *dir;
  struct dirent *entry;
  struct stat status;
  char *filepath;
  int result;

  dir = opendir( path );
  if( !( dir ) )
  {
    bsDailyBackupError( context, "read backup directory", path );
    return 0;
  }
  result = 1;
  errno = 0;
  while( ( entry = readdir( dir ) ) )
  {
    if( bsDailyBackupSnapshotName( entry->d_name ) )
    {
      filepath = ccStrAllocPrintf( "%s/%s", path, entry->d_name );
      if( lstat( filepath, &status ) )
      {
        bsDailyBackupError( context, "inspect old backup", filepath );
        result = 0;
      }
      else if( S_ISREG( status.st_mode ) && ( status.st_mtime < cutoff ) && unlink( filepath ) )
      {
        bsDailyBackupError( context, "remove old backup", filepath );
        result = 0;
      }
      free( filepath );
    }
    errno = 0;
  }
  if( errno )
  {
    bsDailyBackupError( context, "read backup directory", path );
    result = 0;
  }
  if( closedir( dir ) )
  {
    bsDailyBackupError( context, "close backup directory", path );
    result = 0;
  }
  return result;
}


static int bsDailyBackupPrune( bsContext *context, time_t now )
{
  DIR *dir;
  struct dirent *entry;
  struct stat status;
  char *filepath;
  int result, dailyflag, dateflag;
  size_t length;
  time_t cutoff;

  dir = opendir( BS_BACKUP_DIR );
  if( !( dir ) )
  {
    bsDailyBackupError( context, "read backup directory", BS_BACKUP_DIR );
    return 0;
  }
  cutoff = now - (time_t)( 10 * 86400 );
  result = 1;
  errno = 0;
  while( ( entry = readdir( dir ) ) )
  {
    length = strlen( entry->d_name );
    dailyflag = ( length == 20 ) && !( strncmp( entry->d_name, "daily-", 6 ) ) && bsDailyBackupDate( entry->d_name + 6 ) && !( strcmp( entry->d_name + 16, ".tar" ) );
    dateflag = ( length == 10 ) && bsDailyBackupDate( entry->d_name );
    if( dailyflag || dateflag )
    {
      filepath = ccStrAllocPrintf( BS_BACKUP_DIR "/%s", entry->d_name );
      if( lstat( filepath, &status ) )
      {
        bsDailyBackupError( context, "inspect old backup", filepath );
        result = 0;
      }
      else if( dailyflag && S_ISREG( status.st_mode ) && ( status.st_mtime < cutoff ) && unlink( filepath ) )
      {
        bsDailyBackupError( context, "remove old backup", filepath );
        result = 0;
      }
      else if( dateflag && S_ISDIR( status.st_mode ) && !( bsDailyBackupPruneDirectory( context, filepath, cutoff ) ) )
        result = 0;
      free( filepath );
    }
    errno = 0;
  }
  if( errno )
  {
    bsDailyBackupError( context, "read backup directory", BS_BACKUP_DIR );
    result = 0;
  }
  if( closedir( dir ) )
  {
    bsDailyBackupError( context, "close backup directory", BS_BACKUP_DIR );
    result = 0;
  }
  return result;
}


static int bsDailyBackupListRecord( bsContext *context, FILE *list, const char *path, int required )
{
  struct stat status;
  size_t length;

  if( lstat( path, &status ) )
  {
    if( !( required ) && ( errno == ENOENT ) )
      return 1;
    bsDailyBackupError( context, "read saved record", path );
    return 0;
  }
  if( !( S_ISREG( status.st_mode ) ) )
  {
    if( !( required ) )
      return 1;
    errno = EINVAL;
    bsDailyBackupError( context, "archive required regular file", path );
    return 0;
  }
  length = strlen( path ) + 1;
  if( fwrite( path, 1, length, list ) != length )
  {
    bsDailyBackupError( context, "write backup file list for", path );
    return 0;
  }
  return 1;
}


static int bsDailyBackupOrderName( const char *name )
{
  const char *number;
  if( !( strncmp( name, "bricklink-", 10 ) ) )
    number = name + 10;
  else if( !( strncmp( name, "brickowl-", 9 ) ) )
    number = name + 9;
  else
    return 0;
  if( ( *number < '0' ) || ( *number > '9' ) )
    return 0;
  do
    number++;
  while( ( *number >= '0' ) && ( *number <= '9' ) );
  return !( strcmp( number, ".bsx" ) );
}


static int bsDailyBackupListOrders( bsContext *context, FILE *list )
{
  DIR *dir;
  struct dirent *entry;
  struct stat status;
  char *path;
  int result;

  if( lstat( BS_BRICKLINK_ORDER_DIR, &status ) )
  {
    if( errno == ENOENT )
      return 1;
    bsDailyBackupError( context, "inspect saved order directory", BS_BRICKLINK_ORDER_DIR );
    return 0;
  }
  if( !( S_ISDIR( status.st_mode ) ) )
  {
    errno = EINVAL;
    bsDailyBackupError( context, "read saved order directory", BS_BRICKLINK_ORDER_DIR );
    return 0;
  }
  dir = opendir( BS_BRICKLINK_ORDER_DIR );
  if( !( dir ) )
  {
    bsDailyBackupError( context, "read saved order directory", BS_BRICKLINK_ORDER_DIR );
    return 0;
  }
  result = 1;
  errno = 0;
  while( ( entry = readdir( dir ) ) )
  {
    if( bsDailyBackupOrderName( entry->d_name ) )
    {
      path = ccStrAllocPrintf( BS_BRICKLINK_ORDER_DIR "/%s", entry->d_name );
      if( !( bsDailyBackupListRecord( context, list, path, 0 ) ) )
        result = 0;
      free( path );
    }
    errno = 0;
  }
  if( errno )
  {
    bsDailyBackupError( context, "read saved order directory", BS_BRICKLINK_ORDER_DIR );
    result = 0;
  }
  if( closedir( dir ) )
  {
    bsDailyBackupError( context, "close saved order directory", BS_BRICKLINK_ORDER_DIR );
    result = 0;
  }
  return result;
}


static int bsDailyBackupCreate( bsContext *context, char *temppath, char *finalpath )
{
  static const char *optional[] = { BS_TRANSLATION_FILE, BS_AUTOSYNC_LEDGER_FILE, BS_GLOBAL_PATH "bricksync.owlignore.txt", BS_AUTOSYNC_STATUS_FILE, 0 };
  char listpath[] = BS_BACKUP_DIR "/.daily-files.XXXXXX";
  char *argv[] = { "/usr/bin/tar", "-c", "-f", temppath, "--no-mac-metadata", "--no-recursion", "--null", "-T", listpath, 0 };
  struct stat status;
  FILE *list;
  int fd, listfd, result, index, spawnresult, childstatus;
  pid_t child, waited;

  if( lstat( temppath, &status ) == 0 )
  {
    if( !( S_ISREG( status.st_mode ) ) )
    {
      errno = EINVAL;
      bsDailyBackupError( context, "replace temporary archive", temppath );
      return 0;
    }
    if( unlink( temppath ) )
    {
      bsDailyBackupError( context, "remove unfinished archive", temppath );
      return 0;
    }
  }
  else if( errno != ENOENT )
  {
    bsDailyBackupError( context, "inspect temporary archive", temppath );
    return 0;
  }
  fd = open( temppath, O_WRONLY | O_CREAT | O_EXCL, 0600 );
  if( fd < 0 )
  {
    bsDailyBackupError( context, "create temporary archive", temppath );
    return 0;
  }
  if( close( fd ) )
  {
    bsDailyBackupError( context, "close temporary archive", temppath );
    unlink( temppath );
    return 0;
  }
  listfd = mkstemp( listpath );
  if( listfd < 0 )
  {
    bsDailyBackupError( context, "create backup file list", listpath );
    unlink( temppath );
    return 0;
  }
  list = fdopen( listfd, "wb" );
  if( !( list ) )
  {
    bsDailyBackupError( context, "open backup file list", listpath );
    close( listfd );
    unlink( listpath );
    unlink( temppath );
    return 0;
  }
  result = bsDailyBackupListRecord( context, list, BS_CONFIGURATION_FILE, 1 );
  result &= bsDailyBackupListRecord( context, list, BS_STATE_FILE, 1 );
  result &= bsDailyBackupListRecord( context, list, BS_INVENTORY_FILE, 1 );
  for( index = 0 ; optional[index] ; index++ )
    result &= bsDailyBackupListRecord( context, list, optional[index], 0 );
  result &= bsDailyBackupListOrders( context, list );
  if( fclose( list ) )
  {
    bsDailyBackupError( context, "finish backup file list", listpath );
    result = 0;
  }
  if( result )
  {
    spawnresult = posix_spawn( &child, "/usr/bin/tar", 0, 0, argv, environ );
    if( spawnresult )
    {
      errno = spawnresult;
      bsDailyBackupError( context, "start archive creation for", finalpath );
      result = 0;
    }
    else
    {
      do
        waited = waitpid( child, &childstatus, 0 );
      while( ( waited < 0 ) && ( errno == EINTR ) );
      if( waited < 0 )
      {
        bsDailyBackupError( context, "wait for archive creation for", finalpath );
        result = 0;
      }
      else if( !( WIFEXITED( childstatus ) ) || WEXITSTATUS( childstatus ) )
      {
        ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Daily backup: archive creation failed for \"%s\".\n", finalpath );
        result = 0;
      }
    }
  }
  if( unlink( listpath ) )
    bsDailyBackupError( context, "remove backup file list", listpath );
  if( result )
  {
    fd = open( temppath, O_RDONLY | O_NOFOLLOW );
    if( fd < 0 )
    {
      bsDailyBackupError( context, "open completed archive", temppath );
      result = 0;
    }
    else
    {
      if( fstat( fd, &status ) || !( S_ISREG( status.st_mode ) ) || fchmod( fd, 0600 ) || fsync( fd ) )
      {
        bsDailyBackupError( context, "save completed archive", temppath );
        result = 0;
      }
      if( close( fd ) )
      {
        bsDailyBackupError( context, "close completed archive", temppath );
        result = 0;
      }
    }
  }
  if( result && !( journalRenameSync( temppath, finalpath, 0 ) ) )
  {
    bsDailyBackupError( context, "publish completed archive", finalpath );
    result = 0;
  }
  if( result )
    result = bsDailyBackupSyncDirectory( context );
  if( !( result ) )
    unlink( temppath );
  else
    ioPrintf( &context->output, 0, BSMSG_INFO "Daily backup saved at \"%s\".\n", finalpath );
  return result;
}


static void bsDailyBackupCheck( bsContext *context )
{
  struct tm local;
  struct stat status;
  time_t now;
  char day[11], finalpath[sizeof(BS_BACKUP_DIR)+32], temppath[sizeof(BS_BACKUP_DIR)+40];

  now = bsDailyBackupTime();
  if( !( localtime_r( &now, &local ) ) || !( strftime( day, sizeof(day), "%Y-%m-%d", &local ) ) )
  {
    ioPrintf( &context->output, IO_MODEBIT_FLUSH, BSMSG_WARNING "Daily backup: could not determine the local day.\n" );
    return;
  }
  if( stat( BS_BACKUP_DIR, &status ) )
  {
    if( ( errno != ENOENT ) || mkdir( BS_BACKUP_DIR, 0700 ) )
    {
      bsDailyBackupError( context, "open backup directory", BS_BACKUP_DIR );
      return;
    }
  }
  else if( !( S_ISDIR( status.st_mode ) ) )
  {
    errno = EINVAL;
    bsDailyBackupError( context, "use backup directory", BS_BACKUP_DIR );
    return;
  }
  if( strcmp( context->dailybackuppruneday, day ) && bsDailyBackupPrune( context, now ) )
    strcpy( context->dailybackuppruneday, day );
  snprintf( finalpath, sizeof(finalpath), BS_BACKUP_DIR "/daily-%s.tar", day );
  snprintf( temppath, sizeof(temppath), BS_BACKUP_DIR "/.daily-%s.tar.tmp", day );
  if( lstat( finalpath, &status ) == 0 )
  {
    if( !( S_ISREG( status.st_mode ) ) )
    {
      errno = EINVAL;
      bsDailyBackupError( context, "use completed archive", finalpath );
    }
    else
      bsDailyBackupSyncDirectory( context );
    return;
  }
  if( errno != ENOENT )
  {
    bsDailyBackupError( context, "inspect daily archive", finalpath );
    return;
  }
  bsDailyBackupCreate( context, temppath, finalpath );
}


void bsDailyBackupTick( bsContext *context )
{
  if( !( context->autosyncflag ) || ( context->dailybackupcycle == context->autosynccyclecount ) )
    return;
  context->dailybackupcycle = context->autosynccyclecount;
  bsDailyBackupCheck( context );
#ifdef BS_TEST_HOOKS
  {
    char value[64];
    int length;
    length = snprintf( value, sizeof(value), "%ld %d\n", (long)getpid(), context->dailybackupcycle );
    if( ccFileStore( BS_GLOBAL_PATH "temp.test.dailybackup-cycle", value, length, 0 ) )
      rename( BS_GLOBAL_PATH "temp.test.dailybackup-cycle", BS_GLOBAL_PATH "test.dailybackup-cycle" );
  }
#endif
}

#endif
