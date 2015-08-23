from subdaap import utils

from daapserver.utils import generate_persistent_id

import logging

# Logger instance
logger = logging.getLogger(__name__)


class Synchronizer(object):
    """
    Synchronizer class for synchronizing one SubSonic server with one local
    database.
    """

    def __init__(self, state, server, db, connection, index):
        self.state = state
        self.server = server
        self.db = db
        self.connection = connection
        self.index = index

        self.items_by_remote_id = {}
        self.base_container_items_by_item_id = {}
        self.containers_by_remote_id = {}

    def sync(self):
        """
        """

        # Start session
        try:
            with self.db.get_write_cursor() as cursor:
                self.cursor = cursor
                self.cache = {}

                # Determine version numbers
                self.sync_versions()

                # Start synchronizing
                self.sync_database()
                self.sync_base_container()

                if self.items_version != self.state.get("items_version"):
                    self.sync_items()
                    self.state["items_version"] = self.items_version

                if self.containers_version != self.state.get(
                        "containers_version"):
                    self.sync_containers()
                    self.state["containers_version"] = self.containers_version

            # Merge changes into the server
            self.update_server()
        finally:
            # Make sure that everything is cleaned up
            self.cursor = None
            self.cache = None

        # TODO: make dependent on actual changes
        return True

    def update_server(self):
        """
        """

        # Helper methods
        def updated_ids(items):
            for value in items.itervalues():
                if value.get("updated"):
                    yield value["id"]

        def removed_ids(items):
            for value in items.itervalues():
                if "updated" not in value:
                    yield value["id"]

        # Update the server
        server = self.server

        # Databases
        server.databases.update_ids([self.database_id])

        # Items
        database = server.databases[self.database_id]
        database.items.remove_ids(removed_ids(self.items_by_remote_id))
        database.items.update_ids(updated_ids(self.items_by_remote_id))

        # Base container and container items
        database.containers.update_ids([self.base_container_id])
        base_container = database.containers[self.base_container_id]
        base_container.container_items.remove_ids(
            removed_ids(self.base_container_items_by_item_id))
        base_container.container_items.update_ids(
            updated_ids(self.base_container_items_by_item_id))

        # Other containers and container items
        database.containers.remove_ids(
            removed_ids(self.containers_by_remote_id))
        database.containers.update_ids(
            updated_ids(self.containers_by_remote_id))

        for container in self.containers_by_remote_id.itervalues():
            if "updated" not in container:
                updated_ids = container["container_items"]
                container = database.containers[container["id"]]

                container.container_items.remove_ids(container.container_items)
                container.container_items.update_ids(updated_ids)

    def sync_versions(self):
        """
        Read the remote index and playlists. Return their versions, so it can
        be decided if synchronization is required.
        For the index, a `lastModified` field is available in SubSonic's
        response message. This is not the case for playlists, so the naive
        approach is to fetch all playlists, calulate a checksum and compare. A
        request for a similar feature is addressed in
        http://forum.subsonic.org/forum/viewtopic.php?f=3&t=13972.
        Because the index and playlists are reused, they are stored in cache.
        """

        items_version = 0
        containers_version = 0

        # Items
        self.cache["index"] = response = self.connection.getIndexes(
            ifModifiedSince=self.state["items_version"])

        if "lastModified" in response["indexes"]:
            items_version = response["indexes"]["lastModified"]
        else:
            items_version = self.state["items_version"]

        # Playlists
        self.cache["playlists"] = response = self.connection.getPlaylists()

        for playlist in response["playlists"]["playlist"]:
            self.cache["playlist_%d" % playlist["id"]] = response = \
                self.connection.getPlaylist(playlist["id"])

            containers_checksum = utils.dict_checksum(response["playlist"])
            containers_version = (containers_version + containers_checksum) \
                % 0xFFFFFFFF

        # Return version numbers
        self.items_version = items_version
        self.containers_version = containers_version

    def sync_database(self):
        """
        """

        # Calculate checksum
        checksum = utils.dict_checksum(
            name=self.connection.name, remote_id=self.index)

        # Fetch existing item
        try:
            row = self.cursor.query_one(
                """
                SELECT
                    `databases`.`id`,
                    `databases`.`checksum`
                FROM
                    `databases`
                WHERE
                    `databases`.`remote_id` = ?
                """, self.index)
        except IndexError:
            row = None

        # To insert or to update
        if row is None:
            database_id = self.cursor.query(
                """
                INSERT INTO `databases` (
                    `persistent_id`,
                    `name`,
                    `checksum`,
                    `remote_id`)
                VALUES
                    (?, ?, ?, ?)
                """,
                generate_persistent_id(),
                self.connection.name,
                checksum,
                self.index).lastrowid
        elif row["checksum"] != checksum:
            database_id = row["id"]
            self.cursor.query(
                """
                UPDATE
                    `databases`
                SET
                    `name` = ?,
                    `checksum` = ?
                WHERE
                    `databases`.`id` = ?
                """,
                self.connection.name,
                checksum,
                database_id)
        else:
            database_id = row["id"]

        # Update cache
        self.database_id = database_id

    def sync_base_container(self):
        """
        """

        # Calculate checksum
        checksum = utils.dict_checksum(
            is_base=True, is_smart=False, name=self.connection.name)

        # Fetch existing item
        try:
            row = self.cursor.query_one(
                """
                SELECT
                    `containers`.`id`,
                    `containers`.`checksum`
                FROM
                    `containers`
                WHERE
                    `containers`.`database_id` = ? AND
                    `containers`.`is_base` = 1
                """, self.database_id)
        except IndexError:
            row = None

        # To insert or to update
        if row is None:
            base_container_id = self.cursor.query(
                """
                INSERT INTO `containers` (
                   `persistent_id`,
                   `database_id`,
                   `name`,
                   `is_base`,
                   `is_smart`,
                   `checksum`)
                VALUES
                   (?, ?, ?, ?, ?, ?)
                """,
                generate_persistent_id(),
                self.database_id,
                self.connection.name,
                True,
                False,
                checksum).lastrowid
        elif row["checksum"] != checksum:
            base_container_id = row["id"]
            self.cursor.query(
                """
                UPDATE
                    `containers`
                SET
                    `name` = ?,
                    `is_base` = ?,
                    `is_smart` = ?,
                    `checksum` = ?
                WHERE
                    `containers`.`id` = ?
                """,
                self.connection.name,
                True,
                False,
                checksum,
                base_container_id)
        else:
            base_container_id = row["id"]

        # Update cache
        self.base_container_id = base_container_id

    def sync_items(self):
        """
        """

        # Helper methods
        def is_artist_processed(item):
            return item["artistId"] in self.artists_by_remote_id and \
                "updated" in self.artists_by_remote_id[item["artistId"]]

        def is_album_processed(album):
            return album["artistId"] in self.albums_by_remote_id and  \
                "updated" in self.albums_by_remote_id[album["artistId"]]

        def removed_ids(items):
            for value in items.itervalues():
                if "updated" not in value:
                    yield value["id"]

        # Index items, artists, albums and container items by remote IDs.
        self.items_by_remote_id = self.cursor.query_dict(
            """
            SELECT
                `items`.`remote_id`,
                `items`.`id`,
                `items`.`checksum`
            FROM
                `items`
            WHERE
                `items`.`database_id` = ?
            """, self.database_id)
        self.artists_by_remote_id = self.cursor.query_dict(
            """
            SELECT
                `artists`.`remote_id`,
                `artists`.`id`,
                `artists`.`checksum`
            FROM
                `artists`
            WHERE
                `artists`.`database_id` = ?
            """, self.database_id)
        self.albums_by_remote_id = self.cursor.query_dict(
            """
            SELECT
                `albums`.`remote_id`,
                `albums`.`id`,
                `albums`.`checksum`
            FROM
                `albums`
            WHERE
                `albums`.`database_id` = ?
            """, self.database_id)
        self.base_container_items_by_item_id = self.cursor.query_dict(
            """
            SELECT
                `container_items`.`item_id`,
                `container_items`.`id`
            FROM
                `container_items`
            WHERE
                `container_items`.`container_id` = ?
            """, self.base_container_id)

        # Iterate over each item, sync artist, album, item and container item.
        for item in self.walk_index():
            if "artistId" in item:
                if not is_artist_processed(item):
                    self.sync_artist(item)

                    for album in self.walk_artist(item["artistId"]):
                        if not is_album_processed(album):
                            self.sync_album(album)

            self.sync_item(item)
            self.sync_base_container_item(item)

        # Delete old artist, albums, items and container items
        self.cursor.query("""
            DELETE FROM
                `container_items`
            WHERE
                `container_items`.`id` IN (%s)
            """ % utils.in_list(removed_ids(
            self.base_container_items_by_item_id)))
        self.cursor.query("""
            DELETE FROM
                `items`
            WHERE
                `items`.`id` IN (%s)
            """ % utils.in_list(removed_ids(self.items_by_remote_id)))
        self.cursor.query("""
            DELETE FROM
                `artists`
            WHERE
                `artists`.`id` IN (%s)
            """ % utils.in_list(removed_ids(self.artists_by_remote_id)))
        self.cursor.query("""
            DELETE FROM
                `albums`
            WHERE
                `albums`.`id` IN (%s)
            """ % utils.in_list(removed_ids(self.albums_by_remote_id)))

    def sync_item(self, item):
        """
        """

        checksum = utils.dict_checksum(item)
        artist = self.artists_by_remote_id.get(item.get("artistId"))
        album = self.albums_by_remote_id.get(item.get("albumId"))

        # Fetch existing item
        try:
            row = self.items_by_remote_id[item["id"]]
        except KeyError:
            row = None

        # To insert or to update
        updated = True

        if row is None:
            item_id = self.cursor.query(
                """
                INSERT INTO `items` (
                    `persistent_id`,
                    `database_id`,
                    `artist_id`,
                    `album_id`,
                    `name`,
                    `genre`,
                    `year`,
                    `track`,
                    `duration`,
                    `bitrate`,
                    `file_name`,
                    `file_type`,
                    `file_suffix`,
                    `file_size`,
                    `checksum`,
                    `remote_id`)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                generate_persistent_id(),
                self.database_id,
                artist["id"] if artist else None,
                album["id"] if album else None,
                item.get("title"),
                item.get("genre"),
                item.get("year"),
                item.get("track"),
                item["duration"] * 1000 if "duration" in item else None,
                item.get("bitRate"),
                item.get("path"),
                item.get("contentType"),
                item.get("suffix"),
                item.get("size"),
                checksum,
                item["id"]).lastrowid
        elif row["checksum"] != checksum:
            item_id = row["id"]
            self.cursor.query(
                """
                UPDATE
                    `items`
                SET
                    `artist_id` = ?,
                    `album_id` = ?,
                    `name` = ?,
                    `genre` = ?,
                    `year` = ?,
                    `track` = ?,
                    `duration` = ?,
                    `bitrate` = ?,
                    `file_name` = ?,
                    `file_type` = ?,
                    `file_suffix` = ?,
                    `file_size` = ?,
                    `checksum` = ?
                WHERE
                    `items`.`id` = ?
                """,
                artist["id"] if artist else None,
                album["id"] if album else None,
                item.get("title"),
                item.get("genre"),
                item.get("year"),
                item.get("track"),
                item["duration"] * 1000 if "duration" in item else None,
                item.get("bitRate"),
                item.get("path"),
                item.get("contentType"),
                item.get("suffix"),
                item.get("size"),
                checksum,
                item_id)
        else:
            updated = False
            item_id = row["id"]

        # Update cache
        self.items_by_remote_id[item["id"]] = {
            "remote_id": item["id"],
            "id": item_id,
            "checksum": checksum,
            "updated": updated
        }

    def sync_base_container_item(self, item):
        """
        """

        item_row = self.items_by_remote_id[item["id"]]

        # Fetch existing item
        try:
            row = self.base_container_items_by_item_id[item_row["id"]]
        except KeyError:
            row = None

        # To insert or not
        updated = False

        if row is None:
            updated = True
            base_container_item_id = self.cursor.query(
                """
                INSERT INTO `container_items` (
                    `database_id`,
                    `container_id`,
                    `item_id`)
                VALUES
                    (?, ?, ?)
                """,
                self.database_id,
                self.base_container_id,
                item_row["id"]).lastrowid
        else:
            base_container_item_id = row["id"]

        # Update cache
        self.base_container_items_by_item_id[item_row["id"]] = {
            "item_id": item_row["id"],
            "id": base_container_item_id,
            "updated": updated
        }

    def sync_artist(self, item):
        """
        """

        checksum = utils.dict_checksum(name=item["artist"])

        # Fetch existing item
        try:
            row = self.artists_by_remote_id[item["artistId"]]
        except KeyError:
            row = None

        # To insert or to update
        updated = True

        if row is None:
            artist_id = self.cursor.query(
                """
                INSERT INTO `artists` (
                    `database_id`,
                    `name`,
                    `remote_id`,
                    `checksum`)
                VALUES
                    (?, ?, ?, ?)
                """,
                self.database_id,
                item["artist"],
                item["artistId"],
                checksum).lastrowid
        elif row["checksum"] != checksum:
            artist_id = row["id"]
            self.cursor.query(
                """
                UPDATE
                    `artists`
                SET
                    `name` = ?,
                    `checksum` = ?
                WHERE
                    `artists`.`id` = ?
                """,
                item["artist"],
                checksum,
                artist_id)
        else:
            updated = False
            artist_id = row["id"]

        # Update cache
        self.artists_by_remote_id[item["artistId"]] = {
            "remote_id": item["artistId"],
            "id": artist_id,
            "checksum": checksum,
            "updated": updated
        }

    def sync_album(self, album):
        """
        """

        checksum = utils.dict_checksum(album)
        artist_row = self.artists_by_remote_id.get(album.get("artistId"))

        # Fetch existing item
        try:
            row = self.albums_by_remote_id[album["id"]]
        except KeyError:
            row = None

        # To insert or to update
        updated = True

        if row is None:
            album_id = self.cursor.query(
                """
                INSERT INTO `albums` (
                   `database_id`,
                   `artist_id`,
                   `name`,
                   `art`,
                   `checksum`,
                   `remote_id`)
                VALUES
                   (?, ?, ?, ?, ?, ?)
                """,
                self.database_id,
                artist_row["id"] if artist_row else None,
                album["name"],
                "coverArt" in album,
                checksum,
                album["id"]).lastrowid
        elif row["checksum"] != checksum:
            album_id = row["id"]
            self.cursor.query(
                """
                UPDATE
                    `albums`
                SET
                   `name` = ?,
                   `art` = ?,
                   `checksum` = ?
                WHERE
                    `albums`.`id` = ?
                """,
                album["artist"],
                "coverArt" in album,
                checksum,
                album_id)
        else:
            updated = False
            album_id = row["id"]

        # Update cache
        self.albums_by_remote_id[album["id"]] = {
            "remote_id": album["id"],
            "id": album_id,
            "checksum": checksum,
            "updated": updated
        }

    def sync_containers(self):
        """
        """

        def removed_ids(items):
            for value in items.itervalues():
                if "updated" not in value:
                    yield value["id"]

        # Index containers by remote IDs.
        self.containers_by_remote_id = self.cursor.query_dict(
            """
            SELECT
                `containers`.`remote_id`,
                `containers`.`id`,
                `containers`.`checksum`
            FROM
                `containers`
            WHERE
                `containers`.`database_id` = ? AND NOT
                `containers`.`id` = ?
            """, self.database_id, self.base_container_id)

        # Iterate over each playlist.
        for container in self.walk_playlists():
            self.sync_container(container)
            self.sync_container_items(container)

        # Delete old containers and container items.
        self.cursor.query("""
            DELETE FROM
                `containers`
            WHERE
                `containers`.`id` IN (%s)
            """ % utils.in_list(removed_ids(self.containers_by_remote_id)))

    def sync_container(self, container):
        """
        """

        checksum = utils.dict_checksum(
            is_base=False, name=container["name"],
            song_count=container["songCount"])

        # Fetch existing item
        try:
            row = self.containers_by_remote_id[container["id"]]
        except KeyError:
            row = None

        # To insert or to update
        updated = True

        if row is None:
            container_id = self.cursor.query(
                """
                INSERT INTO `containers` (
                   `persistent_id`,
                   `database_id`,
                   `parent_id`,
                   `name`,
                   `is_base`,
                   `is_smart`,
                   `checksum`,
                   `remote_id`)
                VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                generate_persistent_id(),
                self.database_id,
                self.base_container_id,
                container["name"],
                False,
                False,
                checksum,
                container["id"]).lastrowid
        elif row["checksum"] != checksum:
            container_id = row["id"]
            self.cursor.query(
                """
                UPDATE
                    `containers`
                SET
                    `name` = ?,
                    `is_base` = ?,
                    `is_smart` = ?,
                    `checksum` = ?
                WHERE
                    `containers`.`id` = ?
                """,
                container["name"],
                False,
                False,
                checksum,
                container_id)
        else:
            updated = False
            container_id = row["id"]

        # Update cache
        self.containers_by_remote_id[container["id"]] = {
            "remote_id": container["id"],
            "id": container_id,
            "checksum": checksum,
            "updated": updated,
            "container_items": []
        }

    def sync_container_items(self, container):
        """
        """

        # Synchronizing container items is hard. There is no easy way to see
        # what has changed between two containers. Therefore, start by deleting
        # all container items and re-add every item in the specified order.
        self.cursor.query("""
            DELETE FROM
                `container_items`
            WHERE
                `container_items`.`container_id` = ?
            """, self.containers_by_remote_id[container["id"]]["id"])

        for container_item in self.walk_playlist(container["id"]):
            self.sync_container_item(container, container_item)

    def sync_container_item(self, container, container_item):
        """
        """

        item_row = self.items_by_remote_id[container_item["id"]]
        container_id = self.containers_by_remote_id[container["id"]]["id"]

        container_item_id = self.cursor.query(
            """
            INSERT INTO `container_items` (
                `database_id`,
                `container_id`,
                `item_id`,
                `order`)
            VALUES
                (?, ?, ?, ?)
            """,
            self.database_id,
            container_id,
            item_row["id"],
            container_item["order"]).lastrowid

        # Update cache
        self.containers_by_remote_id[container["id"]][
            "container_items"].append(container_item_id)

    def walk_index(self):
        """
        Request SubSonic's index and iterate each item.
        """

        response = self.cache.get("index") or self.connection.getIndexes()

        for index in response["indexes"]["index"]:
            for index in index["artist"]:
                for item in self.walk_directory(index["id"]):
                    yield item

        for child in response["indexes"]["child"]:
            if child.get("isDir"):
                for child in self.walk_directory(child["id"]):
                    yield child
            else:
                yield child

    def walk_playlists(self):
        """
        Request SubSonic's playlists and iterate over each item.
        """

        response = self.cache.get("playlists") or \
            self.connection.getPlaylists()

        for child in response["playlists"]["playlist"]:
            yield child

    def walk_playlist(self, playlist_id):
        """
        Request SubSonic's playlist items and iterate over each item.
        """

        response = self.cache.get("playlist_%d" % playlist_id) or \
            self.connection.getPlaylist(playlist_id)

        for order, child in enumerate(response["playlist"]["entry"], start=1):
            child["order"] = order
            yield child

    def walk_directory(self, directory_id):
        """
        Request a SubSonic music directory and iterate over each item.
        """

        response = self.connection.getMusicDirectory(directory_id)

        for child in response["directory"]["child"]:
            if child.get("isDir"):
                for child in self.walk_directory(child["id"]):
                    yield child
            else:
                yield child

    def walk_artist(self, artist_id):
        """
        Request a SubSonic artist and iterate over each album.
        """

        response = self.connection.getArtist(artist_id)

        for child in response["artist"]["album"]:
            yield child
