from google.appengine.api import memcache

from mlabns.db import model
from mlabns.util import constants
from mlabns.util import distance
from mlabns.util import message

import logging
import random


class ResolverBase:
    """Resolver base class."""

    def get_candidates(self, query):
        """Find candidates for server selection.

        Args:
            query: A LookupQuery instance.

        Returns:
            A list of SliverTool entities that match the requirements
            specified in the 'query'.
        """
        if query.address_family:
            return self._get_candidates(query, query.address_family)
        return []

    def _get_candidates(self, query, address_family):
        # First try to get the sliver tools from the cache.
        sliver_tools = memcache.get(query.tool_id)
        if sliver_tools:
            logging.info(
                'Sliver tools found in memcache (%s results).',
                len(sliver_tools))
            candidates = []
            for sliver_tool in sliver_tools:
                if (address_family == message.ADDRESS_FAMILY_IPv4 and
                    sliver_tool.status_ipv4 == message.STATUS_ONLINE) or \
                    (address_family == ADDRESS_FAMILY_IPv6 and
                    sliver_tool.status_ipv6 == message.STATUS_ONLINE):
                    candidates.append(sliver_tool)
            return candidates

        logging.info('Sliver tools not found in memcache.')
        # Get the sliver tools from from datastore.
        status_field = 'status_' + address_family
        candidates = model.SliverTool.gql(
            'WHERE tool_id = :tool_id '
            'AND ' + status_field + ' = :status',
            tool_id=query.tool_id,
            status=message.STATUS_ONLINE)

        logging.info(
            'Found (%s candidates)', candidates.count())
        return candidates.fetch(constants.MAX_FETCHED_RESULTS)

    def answer_query(self, query):
        pass


class GeoResolver(ResolverBase):
    """Chooses the server geographically closest to the client."""

    def answer_query(self, query):
        """Selects the geographically closest SliverTool.

        Args:
            query: A LookupQuery instance.

        Returns:
            A SliverTool entity in case of success, or None if there is no
            SliverTool available that matches the query.
        """
        candidates = self.get_candidates(query)
        if not candidates:
            logging.error('No results found for %s.', query.tool_id)
            return None

        if not query.latitude or not query.longitude:
            logging.warning(
                'No geolocation info, returning a random sliver tool')
            return random.choice(candidates)

        logging.info('Found %s candidates.', len(candidates))
        min_distance = float('+inf')
        closest_sliver_tools = []
        distances = {}

        # Compute for each SliverTool the distance and add the SliverTool
        # to the 'closest_sliver_tools' list if the computed  distance is
        # less (or equal) than the current minimum.
        # To avoid computing the distances twice, cache the results in
        # a dict.
        for sliver_tool in candidates:
            # Check if we already computed this distance.
            if (distances.has_key(sliver_tool.site_id)):
                current_distance = distances[sliver_tool.site_id]
            else:
                current_distance = distance.distance(
                    query.latitude,
                    query.longitude,
                    sliver_tool.latitude,
                    sliver_tool.longitude)

                # Update the dict.
                if sliver_tool.site_id not in distances:
                    distances[sliver_tool.site_id] = current_distance

            # Update the min distance and add the SliverTool to the list.
            if (current_distance <= min_distance):
                min_distance = current_distance
                closest_sliver_tools.insert(0, sliver_tool)

        # Sort the 'closest_sliver_tools' list by distance and select only
        # those within an acceptable range. Then return one of these,
        # chosen randomly.
        best_sliver_tools = []
        distance_range = min_distance
        for sliver_tool in closest_sliver_tools:
            if  distances[sliver_tool.site_id] <= distance_range:
                best_sliver_tools.append(sliver_tool)
            else:
                break

        return random.choice(best_sliver_tools)

class MetroResolver(ResolverBase):
    """Implements the metro policy."""

    def _get_candidates(self, query, address_family):
        # TODO(claudiu) Test whether the following query works as expected:
        # sites = model.Site.gql("WHERE metro = :metro", metro=query.metro)
        sites = model.Site.all().filter("metro =", query.metro).fetch(
            constants.MAX_FETCHED_RESULTS)

        logging.info(
            'Found %s results for metro %s.', len(sites), query.metro)
        if not sites:
            logging.info('No results found for metro %s.', query.metro)
            return None

        site_id_list = []
        for site in sites:
            site_id_list.append(site.site_id)

        status_field = 'status_' + address_family
        # TODO(claudiu) Use the memcache.
        candidates = model.SliverTool.gql(
            'WHERE tool_id = :tool_id '
            'AND ' + status_field + ' = :status '
            'AND site_id in :site_id_list',
            tool_id=query.tool_id,
            status=message.STATUS_ONLINE,
            site_id_list=site_id_list).fetch(constants.MAX_FETCHED_RESULTS)

        return candidates

    def answer_query(self, query):
        """Selects randomly a sliver tool that matches the 'metro'.

        Args:
            query: A LookupQuery instance.

        Returns:
            A SliverTool entity that matches the 'metro' if available,
            and None otherwise.
        """
        candidates = self.get_candidates(query)
        if not candidates:
            logging.error('No results found for %s.', query.tool_id)
            return None

        return random.choice(candidates)

class RandomResolver(ResolverBase):
    """Returns a server chosen randomly."""

    def answer_query(self, query):
        """Returns a randomly chosen SliverTool.

        Args:
            query: A LookupQuery instance.

        Returns:
            A SliverTool entity if available, None otherwise.
        """
        candidates = self.get_candidates(query)
        if not candidates:
            logging.error('No results found for %s.', query.tool_id)
            return None

        return random.choice(candidates)


class CountryResolver(ResolverBase):
    """Returns a server in a specified country."""

    def answer_query(self, query):
        """Returns a SliverTool in a specified country.

        Args:
            query: A LookupQuery instance.

        Returns:
            A SliverTool entity if available, None otherwise.
        """
        if not query.user_defined_country:
            return None

        candidates = self.get_candidates(query)
        if not candidates:
            logging.error('No results found for %s.', query.tool_id)
            return None

        country_candidates = []
        for candidate in candidates:
            if candidate.country == query.user_defined_country:
                country_candidates.append(candidate)

        if not country_candidates:
            return None
        return random.choice(country_candidates)


def new_resolver(policy):
    if policy == message.POLICY_GEO:
        return GeoResolver()
    elif policy == message.POLICY_METRO:
        return MetroResolver()
    elif policy == message.POLICY_RANDOM:
        return RandomResolver()
    elif policy == message.POLICY_COUNTRY:
        return CountryResolver()
    else:
        return RandomResolver()
