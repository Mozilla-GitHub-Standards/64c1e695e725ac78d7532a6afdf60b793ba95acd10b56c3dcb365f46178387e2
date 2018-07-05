import time

from jx_python import jx
from mo_dots import Null, coalesce, wrap
from mo_future import text_type
from mo_hg.hg_mozilla_org import HgMozillaOrg
from mo_files.url import URL
from mo_logs import Log
from mo_threads import Till, Thread, Lock
from pyLibrary.env import http
from pyLibrary.sql import sql_list, sql_iso
from pyLibrary.sql.sqlite import quote_value
from tuid import sql

# Use import as follows to prevent
# circular dependency conflict for
# TUIDService, which makes use of the
# Clogger
import tuid.service


RETRY = {"times": 3, "sleep": 5}
SQL_CSET_BATCH_SIZE = 500
CSET_TIP_WAIT_TIME = 5 * 60 # seconds
CSET_BACKFILL_WAIT_TIME = 1 * 60 # seconds
CSET_MAINTENANCE_WAIT_TIME = 30 * 60 # seconds
CSET_DELETION_WAIT_TIME = 1 * 60 # seconds
TUID_EXISTENCE_WAIT_TIME = 1 * 60 # seconds
MINIMUM_PERMANENT_CSETS = 1000
MAXIMUM_NONPERMANENT_CSETS = 20000
UPDATE_VERY_OLD_FRONTIERS = False

HG_URL = URL('https://hg.mozilla.org/')


class Clogger:
    def __init__(self, conn=None, tuid_service=None, kwargs=None):
        try:
            self.config = kwargs

            self.conn = conn if conn else sql.Sql(self.config.database.name)
            self.hg_cache = HgMozillaOrg(kwargs=self.config.hg_cache, use_cache=True) if self.config.hg_cache else Null

            self.tuid_service = tuid_service if tuid_service else tuid.service.TUIDService(
                database=None, hg=None, kwargs=self.config, conn=self.conn, clogger=self
            )
            self.rev_locker = Lock()
            self.working_locker = Lock()

            self.init_db()
            self.next_revnum = coalesce(self.conn.get_one("SELECT max(revnum)+1 FROM csetLog")[0], 1)
            self.csets_todo_backwards = []
            self.deletions_todo = []
            self.at_tip = True
            self.config = self.config.tuid

            self.disable_backfilling = False
            self.disable_tipfilling = False
            self.disable_deletion = False
            self.disable_maintenance = False

            # Make sure we are filled before allowing queries
            numrevs = self.conn.get_one("SELECT count(revnum) FROM csetLog")[0]
            print("csetLog has " + str(numrevs))
            if numrevs < MINIMUM_PERMANENT_CSETS:
                Log.note("Filling in csets to hold {{minim}} csets.", minim=MINIMUM_PERMANENT_CSETS)
                oldest_rev = 'tip'
                with self.conn.transaction() as t:
                    tmp = t.query("SELECT min(revnum), revision FROM csetLog").data[0][1]
                    if tmp:
                        oldest_rev = tmp
                self._fill_in_range(
                    MINIMUM_PERMANENT_CSETS - numrevs,
                    oldest_rev,
                    timestamp=False
                )

            Log.note(
                "Table is filled with atleast {{minim}} entries. Starting workers...",
                minim=MINIMUM_PERMANENT_CSETS
            )

            Thread.run('clogger-tip', self.fill_forward_continuous)
            Thread.run('clogger-backfill', self.fill_backward_with_list)
            Thread.run('clogger-maintenance', self.csetLog_maintenance)
            Thread.run('clogger-deleter', self.csetLog_deleter)

            Log.note("Started clogger workers.")
        except Exception as e:
            Log.error("Can not setup clogger: {{cause}}", cause=e)


    def init_db(self):
        with self.conn.transaction() as t:
            t.execute('''
            CREATE TABLE IF NOT EXISTS csetLog (
                revnum         INTEGER PRIMARY KEY,
                revision       CHAR(12) NOT NULL,
                timestamp      INTEGER
            );''')


    def revnum(self):
        """
        :return: next tuid
        """
        return self.conn.get_one("SELECT max(revnum) as revnum FROM csetLog")[0]


    def get_tip(self, transaction):
        return transaction.get_one(
            "SELECT max(revnum) as revnum, revision FROM csetLog"
        )


    def get_tail(self, transaction):
        return transaction.get_one(
            "SELECT min(revnum) as revnum, revision FROM csetLog"
        )

    def _get_clog(self, clog_url):
        try:
            Log.note("Searching through changelog {{url}}", url=clog_url)
            clog_obj = http.get_json(clog_url, retry=RETRY)
            return clog_obj
        except Exception as e:
            Log.error(
                "Unexpected error getting changset-log for {{url}}: {{error}}",
                url=clog_url,
                error=e
            )


    def _get_one_revision(self, transaction, cset_entry):
        # Returns a single revision if it exists
        _, rev, _ = cset_entry
        return transaction.get_one("SELECT revision FROM csetLog WHERE revision=?", (rev,))


    def _get_one_revnum(self, transaction, rev):
        # Returns a single revnum if it exists
        return transaction.get_one("SELECT revnum FROM csetLog WHERE revision=?", (rev,))


    def _get_revnum_range(self, transaction, revnum1, revnum2):
        # Returns a range of revision numbers (that is inclusive)
        high_num = max(revnum1, revnum2)
        low_num = min(revnum1, revnum2)

        return transaction.query(
            "SELECT revnum, revision FROM csetLog WHERE revnum >= ? AND revnum <= ?",
            (low_num, high_num)
        )


    def recompute_table_revnums(self):
        '''
        Recomputes the revnums for the csetLog table
        by creating a new table, and copying csetLog to
        it. The INTEGER PRIMARY KEY in the temp table auto increments
        as rows are added.

        IMPORTANT: Only call this after acquiring the
                   lock `self.working_locker`.
        :return:
        '''
        with self.conn.transaction() as t:
            t.execute('''
            CREATE TABLE temp (
                revnum         INTEGER PRIMARY KEY,
                revision       CHAR(12) NOT NULL,
                timestamp      INTEGER
            );''')

            t.execute(
                "INSERT INTO temp (revision, timestamp) "
                "SELECT revision, timestamp FROM csetlog ORDER BY revnum ASC"
            )

            t.execute("DROP TABLE csetLog;")
            t.execute("ALTER TABLE temp RENAME TO csetLog;")


    def add_cset_entries(self, ordered_rev_list, timestamp=False, number_forward=True):
        '''
        Adds a list of revisions to the table. Assumes ordered_rev_list is an ordered
        based on how changesets are found in the changelog. Going forwards or backwards is dealt
        with by flipping the list
        :param ordered_cset_list: Order given from changeset log searching.
        :param timestamp: If false, records are kept indefinitely
                          but if holes exist: (delete, None, delete, None)
                          those delete's with None's around them
                          will not be deleted.
        :param numbered: If True, this function will number the revision list
                         by going forward from max(revNum), else it'll go backwards
                         from revNum, then add X to all revnums and self.next_revnum
                         where X is the length of ordered_rev_list
        :return:
        '''
        if number_forward:
            ordered_rev_list = ordered_rev_list[::-1]

        insert_list = []

        # Format insertion list
        for count, rev in enumerate(ordered_rev_list):
            tstamp = -1
            if timestamp:
                tstamp = int(time.time())

            if number_forward:
                revnum = self.revnum()
            else:
                revnum = -count

            cset_entry = (revnum, rev, tstamp)
            insert_list.append(cset_entry)

        with self.conn.transaction() as t:
            # In case of overlapping requests
            fmt_insert_list = []
            for cset_entry in insert_list:
                tmp = self._get_one_revision(t, cset_entry)
                if not tmp:
                    fmt_insert_list.append(cset_entry)

            for _, tmp_insert_list in jx.groupby(fmt_insert_list, size=SQL_CSET_BATCH_SIZE):
                t.execute(
                    "INSERT INTO csetLog (revnum, revision, timestamp)" +
                    " VALUES " +
                    sql_list(
                        sql_iso(
                            sql_list(map(quote_value, (revnum, revision, timestamp)))
                        ) for revnum, revision, timestamp in tmp_insert_list
                    )
                )

        # Move the revision numbers forward if needed
        self.recompute_table_revnums()


    def _fill_in_range(self, parent_cset, child_cset, timestamp=False, number_forward=True):
        '''
        Fills cset logs in a certain range. 'parent_cset' can be an int and in that case,
        we get that many changesets instead. If parent_cset is an int, then we consider
        that we are going backwards (number_forward is False) and we ignore the first
        changeset of the first log, and we ignore the setting for number_forward.
        :param parent_cset:
        :param child_cset:
        :param timestamp:
        :param number_forward:
        :return:
        '''
        csets_to_add = []
        found_parent = False
        find_parent = False
        if type(parent_cset) != int:
            find_parent = True

        csets_found = 0
        final_rev = child_cset
        while not found_parent:
            clog_url = HG_URL / self.config.hg.branch / 'json-log' / final_rev
            clog_obj = self._get_clog(clog_url)
            clog_csets_list = list(clog_obj['changesets'])
            for clog_cset in clog_csets_list[:-1]:
                if not number_forward and csets_found <= 0:
                    # Skip this entry it already exists
                    csets_found += 1
                    continue

                nodes_cset = clog_cset['node'][:12]
                if find_parent:
                    if nodes_cset == parent_cset:
                        found_parent = True
                        if not number_forward:
                            # When going forward this entry is
                            # the given parent
                            csets_to_add.append(nodes_cset)
                        break
                else:
                    if csets_found + 1 > parent_cset:
                        found_parent = True
                        if not number_forward:
                            # When going forward this entry is
                            # the given parent (which is supposed
                            # to already exist)
                            csets_to_add.append(nodes_cset)
                        break
                    csets_found += 1
                csets_to_add.append(nodes_cset)
            if found_parent == True:
                break
            final_rev = clog_csets_list[-1]['node'][:12]

        self.add_cset_entries(csets_to_add, timestamp=timestamp, number_forward=number_forward)
        return csets_to_add


    def fill_backward_with_list(self, please_stop=None):
        '''
        Expects requests of the tuple form: (parent_cset, timestamp)
        parent_cset can be an int X to go back by X changesets, or
        a string to search for going backwards in time. If timestamp
        is false, no timestamps will be added to the entries.
        :param please_stop:
        :return:
        '''
        try:
            while not please_stop:
                if len(self.csets_todo_backwards) <= 0 or self.disable_backfilling:
                    (please_stop | Till(seconds=CSET_BACKFILL_WAIT_TIME)).wait()
                    continue

                with self.working_locker:
                    done = []
                    for parent_cset, timestamp in self.csets_todo_backwards:
                        with self.conn.transaction() as t:
                            parent_revnum = self._get_one_revnum(t, parent_cset)
                        if parent_revnum:
                            done.append((parent_cset, timestamp))
                            continue

                        with self.conn.transaction() as t:
                            oldest_revision = t.query("SELECT min(revNum), revision FROM csetLog").data[0][1]

                        self._fill_in_range(
                            parent_cset,
                            oldest_revision,
                            timestamp=timestamp,
                            number_forward=False
                        )
                        done.append((parent_cset,timestamp))
                        Log.note("Finished {{cset}}", cset=parent_cset)
                    self.csets_todo_backwards = []
        except Exception as e:
            Log.warning("Unknown error occurred during backfill: ", cause=e)


    def update_tip(self):
        '''
        Returns False if the tip is already at the newest, or True
        if an update has taken place.
        :return:
        '''
        clog_obj = self._get_clog(HG_URL / self.config.hg.branch / 'json-log' / 'tip')

        # Get current tip in DB
        with self.conn.transaction() as t:
            newest_known_rev = t.query("SELECT max(revnum) AS revnum, revision FROM csetLog").data[0][1]

        # If we are still at the newest, wait for CSET_TIP_WAIT_TIME seconds
        # before checking again.
        first_clog_entry = clog_obj['changesets'][0]['node'][:12]
        if newest_known_rev == first_clog_entry:
            return False

        with self.working_locker:
            self.at_tip = False
            csets_to_gather = None
            if not newest_known_rev:
                Log.note(
                    "No revisions found in table, adding {{minim}} entries...",
                    minim=MINIMUM_PERMANENT_CSETS
                )
                csets_to_gather = MINIMUM_PERMANENT_CSETS

            found_newest_known = False
            csets_to_add = []
            csets_found = 0
            Log.note("Found new revisions. Updating csetLog tip to {{rev}}...", rev=first_clog_entry)
            while not found_newest_known:
                clog_csets_list = list(clog_obj['changesets'])
                for clog_cset in clog_csets_list[:-1]:
                    nodes_cset = clog_cset['node'][:12]
                    if not csets_to_gather:
                        if nodes_cset == newest_known_rev:
                            found_newest_known = True
                            break
                    else:
                        if csets_found >= csets_to_gather:
                            found_newest_known = True
                            break
                    csets_found += 1
                    csets_to_add.append(nodes_cset)
                if not found_newest_known:
                    # Get the next page
                    final_rev = clog_csets_list[-1]['node'][:12]
                    clog_url = HG_URL / self.config.hg.branch / 'json-log' / final_rev
                    clog_obj = self._get_clog(clog_url)

            Log.note("Adding {{csets}}", csets=csets_to_add)
            self.add_cset_entries(csets_to_add, timestamp=False)
            self.at_tip = True
        return True


    def fill_forward_continuous(self, please_stop=None):
        try:
            while not please_stop:
                waiting_a_bit = False
                if self.disable_backfilling:
                    waiting_a_bit = True

                if not waiting_a_bit:
                    # If an update was done, check if there are
                    # more changesets that have arrived just in case,
                    # otherwise, we wait.
                    did_an_update = self.update_tip()
                    if not did_an_update:
                        waiting_a_bit = True

                if waiting_a_bit:
                    (please_stop | Till(seconds=CSET_TIP_WAIT_TIME)).wait()
                    continue
        except Exception as e:
            Log.warning("Unknown error occurred during tip maintenance:", cause=e)


    def csetLog_maintenance(self, please_stop=None):
        '''
        Handles deleting old csetLog entries and timestamping
        revisions once they pass the length for permanent
        storage for deletion later.
        :param please_stop:
        :return:
        '''
        while not please_stop:
            try:
                # Wait a bit for maintenance cycle to begin
                Till(seconds=CSET_MAINTENANCE_WAIT_TIME).wait()
                if len(self.deletions_todo) > 0 or self.disable_maintenance:
                    continue

                with self.working_locker:
                    all_data = None
                    with self.conn.transaction() as t:
                        all_data = sorted(
                            t.get("SELECT revnum, revision, timestamp FROM csetLog"),
                            key=lambda x: int(x[0])
                        )

                    # Restore maximum permanents (if overflowing)
                    new_data = []
                    modified = False
                    for count, (revnum, revision, timestamp) in enumerate(all_data[::-1]):
                        if count < MINIMUM_PERMANENT_CSETS:
                            if timestamp != -1:
                                modified = True
                                new_data.append((revnum, revision, -1))
                            else:
                                new_data.append((revnum, revision, timestamp))
                        elif type(timestamp) != int:
                            modified = True
                            new_data.append((revnum, revision, int(time.time())))
                        else:
                            new_data.append((revnum, revision, timestamp))

                    # Delete any overflowing entries
                    new_data2 = new_data
                    deleted_data = all_data[:len(all_data) - MAXIMUM_NONPERMANENT_CSETS]
                    delete_overflowing_revstart = None
                    if len(deleted_data) > 0:
                        _, delete_overflowing_revstart, _ = deleted_data[-1]
                        new_data2 = set(all_data) - set(deleted_data)

                        # Update old frontiers if requested, otherwise
                        # they will all get deleted by the csetLog_deleter
                        # worker
                        if UPDATE_VERY_OLD_FRONTIERS:
                            _, max_revision, _ = all_data[-1]
                            for _, revision, _ in deleted_data:
                                with self.conn.transaction() as t:
                                    old_files = t.get(
                                        "SELECT file FROM latestFileMod WHERE revision=?",
                                        (revision,)
                                    )
                                if old_files is None or len(old_files) <= 0:
                                    continue

                                self.tuid_service.get_tuids_from_files(
                                    old_files, max_revision, going_forward=True,
                                )

                                still_exist = True
                                while still_exist:
                                    Till(seconds=TUID_EXISTENCE_WAIT_TIME).wait()
                                    with self.conn.transaction() as t:
                                        old_files = t.get(
                                            "SELECT file FROM latestFileMod WHERE revision=?",
                                            (revision,)
                                        )
                                    if old_files is None or len(old_files) <= 0:
                                        still_exist = False

                    # Update table and schedule a deletion
                    if modified:
                        with self.conn.transaction() as t:
                            t.execute(
                                "INSERT OR REPLACE INTO csetLog (revnum, revision, timestamp) VALUES " +
                                sql_list(
                                    sql_iso(sql_list(map(quote_value, cset_entry)))
                                    for cset_entry in new_data2
                                )
                            )
                    if not deleted_data:
                        continue

                    Log.note("Scheduling {{num_csets}} for deletion", num_csets=len(deleted_data))
                    self.deletions_todo.append(delete_overflowing_revstart)
            except Exception as e:
                Log.warning("Unexpected error occured while maintaining csetLog, continuing to try: ", cause=e)
        return


    def csetLog_deleter(self, please_stop=None):
        '''
        Deletes changesets from the csetLog table
        and also changesets from the annotation table
        that have revisions matching the given changesets.
        Accepts lists of csets from self.deletions_todo.
        :param please_stop:
        :return:
        '''
        while not please_stop:
            try:
                if len(self.deletions_todo) <= 0 or self.disable_deletion:
                    if not self.disable_deletion:
                        Log.note(
                            "Did not find any deletion requests, waiting for {{secs}} to check again.",
                            secs=CSET_DELETION_WAIT_TIME
                        )
                    Till(seconds=CSET_DELETION_WAIT_TIME).wait()
                    continue

                Log.note("Waiting for locks...")
                with self.working_locker:
                    Log.note("Locks acquired.")
                    tmp_deletions = self.deletions_todo
                    for first_cset in tmp_deletions:
                        with self.conn.transaction() as t:
                            revnum = self._get_one_revnum(t, first_cset)[0]
                            print("revnum:" + str(revnum))
                            csets_to_del = t.get(
                                "SELECT revnum, revision FROM csetLog WHERE revnum <= ?", (revnum,)
                            )
                            csets_to_del = [cset for _, cset in csets_to_del]
                            print(csets_to_del)
                            existing_frontiers = t.query(
                                "SELECT revision FROM latestFileMod WHERE revision IN " +
                                sql_iso(sql_list(map(quote_value, csets_to_del)))
                            ).data

                        existing_frontiers = [existing_frontiers[i][0] for i, _ in enumerate(existing_frontiers)]

                        Log.note(
                            "Deleting all annotations and changeset log entries with revisions in the list: {{csets}}",
                            csets=csets_to_del
                        )

                        if len(existing_frontiers) > 0:
                            # This handles files which no longer exist anymore in
                            # the main branch.
                            Log.note("Deleting existing frontiers for files: {{files}}", files=existing_frontiers)
                            with self.conn.transaction() as t:
                                t.execute(
                                    "DELETE FROM latestFileMod WHERE revision IN " +
                                    sql_iso(sql_list(map(quote_value, existing_frontiers)))
                                )

                        with self.conn.transaction() as t:
                            Log.note("Deleting annotations...")
                            t.execute(
                                "DELETE FROM annotations WHERE revision IN " +
                                sql_iso(sql_list(map(quote_value, csets_to_del)))
                            )

                            Log.note(
                                "Deleting {{num_entries}} csetLog entries...",
                                num_entries=len(csets_to_del)
                            )
                            t.execute(
                                "DELETE FROM csetLog WHERE revision IN " +
                                sql_iso(sql_list(map(quote_value, csets_to_del)))
                            )

                        # Recalculate the revnums
                        self.recompute_table_revnums()
                    self.deletions_todo = [todo for todo in self.deletions_todo if todo not in tmp_deletions]
            except Exception as e:
                Log.warning("Unexpected error occured while deleting from csetLog:", cause=e)
                Till(seconds=CSET_DELETION_WAIT_TIME).wait()
        return


    def get_old_cset_revnum(self, revision):
        self.csets_todo_backwards.append((
            revision,
            True
        ))

        revnum = None
        not_in_db = True
        while not_in_db:
            with self.conn.transaction() as t:
                revnum = self._get_one_revnum(t, (revision,))
            if revnum:
                not_in_db = False
            else:
                Log.note("Waiting for backfill to complete...")
                Till(seconds=CSET_BACKFILL_WAIT_TIME).wait()
        return revnum


    def get_revnnums_from_range(self, revision1, revision2):
        with self.conn.transaction() as t:
            revnum1 = self._get_one_revnum(t, revision1)
            revnum2 = self._get_one_revnum(t, revision2)

        if revnum1 is None:
            did_an_update = self.update_tip()
            if did_an_update:
                with self.conn.transaction() as t:
                    revnum1 = self._get_one_revnum(t, revision1)
                    revnum2 = self._get_one_revnum(t, revision2)

            if revnum1 is None:
                revnum1 = self.get_old_cset_revnum(revision1)

                # Refresh the second entry
                with self.conn.transaction() as t:
                    revnum2 = self._get_one_revnum(t, revision2)

        if revnum2 is None:
            revnum2 = self.get_old_cset_revnum(revision2)

        with self.conn.transaction() as t:
            result = self._get_revnum_range(t, revnum1, revnum2)
        return sorted(
            result,
            key=lambda x: int(x[0])
        )