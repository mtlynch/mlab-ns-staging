from google.appengine.api import memcache

from mlabns.db import model
from mlabns.util import constants
from mlabns.util import distance
from mlabns.util import message
from mlabns.util import sliver_tool_distance

import logging
import math
import random
from operator import attrgetter


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
        candidates = []
        if query.address_family is not None:
            candidates = self._get_candidates(query, query.address_family)
        # If no candidates with this address family and if this address family
        # was not user-defined, try the other address family.
        if len(candidates) == 0 and \
            query.address_family != query.user_defined_af:
            if query.address_family == message.ADDRESS_FAMILY_IPv4:
                candidates = self._get_candidates(query,
                                                  message.ADDRESS_FAMILY_IPv6)
            elif query.address_family == message.ADDRESS_FAMILY_IPv6:
                candidates = self._get_candidates(query,
                                                  message.ADDRESS_FAMILY_IPv4)
        return candidates

    def _get_candidates(self, query, address_family):
        """Returns a (possibly empty) list of available candidates."""
        logging.info('Looking for %s in memcache.', query.tool_id)
        # First try to get the sliver tools from the memcache.
        sliver_tools = memcache.get(
            query.tool_id, namespace=constants.MEMCACHE_NAMESPACE_TOOLS)
        if sliver_tools is not None:
            logging.info('Sliver tools found in memcache (%s results).',
                         len(sliver_tools))
            candidates = []
            for sliver_tool in sliver_tools:
                if (address_family == message.ADDRESS_FAMILY_IPv4 and
                    sliver_tool.status_ipv4 == message.STATUS_ONLINE) or \
                    (address_family == message.ADDRESS_FAMILY_IPv6 and
                    sliver_tool.status_ipv6 == message.STATUS_ONLINE):
                    candidates.append(sliver_tool)
            logging.info('After filtering, %d candidates match criteria.',
                         len(candidates))
            return candidates
        logging.info(
            'Sliver tools not found in memcache, falling back to data store.')

        # Get the sliver tools from datastore.
        status_field = 'status_' + address_family
        candidates = model.SliverTool.gql(
            'WHERE tool_id = :tool_id '
            'AND ' + status_field + ' = :status',
            tool_id=query.tool_id,
            status=message.STATUS_ONLINE)
        logging.info('Found %s candidates in data store', candidates.count())
        return candidates.fetch(constants.MAX_FETCHED_RESULTS)

    def _get_candidates_from_sites(self, query, address_family, site_id_list):
        """Returns a (possibly empty) list of available candidates."""
        logging.info('Looking for %s in memcache', query.tool_id)
        # First try to get the sliver tools from the cache.
        sliver_tools = memcache.get(
            query.tool_id, namespace=constants.MEMCACHE_NAMESPACE_TOOLS)
        if sliver_tools is not None:
            logging.info('Sliver tools found in memcache (%s results).',
                         len(sliver_tools))
            candidates = []
            for sliver_tool in sliver_tools:
                if sliver_tool.site_id in site_id_list and \
                    ((address_family == message.ADDRESS_FAMILY_IPv4 and
                    sliver_tool.status_ipv4 == message.STATUS_ONLINE) or
                    (address_family == message.ADDRESS_FAMILY_IPv6 and
                    sliver_tool.status_ipv6 == message.STATUS_ONLINE)):
                    candidates.append(sliver_tool)
            logging.info('After filtering, %d candidates match criteria.',
                         len(candidates))
            return candidates
        logging.info(
            'Sliver tools not found in memcache, falling back to data store.')

        # Get the sliver tools from datastore.
        status_field = 'status_' + address_family
        candidates = model.SliverTool.gql(
            'WHERE tool_id = :tool_id '
            'AND ' + status_field + ' = :status '
            'AND site_id in :site_id_list',
            tool_id=query.tool_id,
            status=message.STATUS_ONLINE,
            site_id_list=site_id_list)
        logging.info('Found %s candidates in data store', candidates.count())
        return candidates.fetch(constants.MAX_FETCHED_RESULTS)

    def answer_query(self, query):
        """Selects a random sliver tool among the available candidates.

        Args:
            query: A LookupQuery instance.

        Returns:
            A SliverTool entity if any available, None otherwise.
        """
        candidates = self.get_candidates(query)
        if len(candidates) == 0:
            logging.error('No results found for %s.', query.tool_id)
            return None

        return [random.choice(candidates)]

class AllResolver(ResolverBase):
    def answer_query(self, query):
        candidates = self.get_candidates(query)
        if len(candidates) == 0:
            logging.error('No results found for %s.', query.tool_id)
            return None
        return candidates

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
        if len(candidates) == 0:
            logging.error('No results found for %s.', query.tool_id)
            return None

        if (query.latitude is None) or (query.longitude is None):
            logging.warning('No latide/longitude, return a random sliver tool.')
            return [random.choice(candidates)]

        min_distance = float('+inf')
        closest_sliver_tools = []
        distances = {}

        # Compute for each SliverTool the distance and add keep in the
        # 'closest_sliver_tools' list only the SliverTools whose distance is
        # less or equal than the current minimum.
        for sliver_tool in candidates:
            # Check if we already computed the distance of this site.
            if distances.has_key(sliver_tool.site_id):
                current_distance = distances[sliver_tool.site_id]
            else:
                current_distance = distance.distance(
                    query.latitude,
                    query.longitude,
                    sliver_tool.latitude,
                    sliver_tool.longitude)

                distances[sliver_tool.site_id] = current_distance

            # Update the min distance and add the SliverTool to the list.
            if current_distance < min_distance:
                min_distance = current_distance
                closest_sliver_tools = [sliver_tool]
            elif current_distance == min_distance:
                closest_sliver_tools.append(sliver_tool)

        # Add the min_distance to the query so it can be logged later. Round to
        # the next highest kilometre radius to remove precision.
        query.distance = math.ceil(min_distance)

        # Choose randomly among candidates with the same, minimum distance.
        return [random.choice(closest_sliver_tools)]


class GeoResolverWithOptions(ResolverBase):
    """Chooses the N geographically closest servers to the client."""

    def answer_query(self, query):
        """Selects the top N geographically closest SliverTools to the client.

        Finds the top N closest SliverTools to the client and returns them.
        Note that N is currently hardcoded to 4.

        Args:
            query: A LookupQuery instance.

        Returns:
            A list of SliverTool entities on success, or None if there is no
            SliverTool available that matches the query.
        """
        # Return no more than MAX_RESULTS SliverTools in the result.
        MAX_RESULTS = 4
        candidates = self.get_candidates(query)
        if len(candidates) == 0:
            logging.error('No results found for %s.', query.tool_id)
            return None

        if (query.latitude is None) or (query.longitude is None):
            logging.warning('No latide/longitude, return a random sliver tool.')
            return [random.choice(candidates)]

        min_distance = float('+inf')
        sliver_tool_bins = {}
        sliver_tools = []
        distances = {}

        # Combine the candidates into bins by site.
        for sliver_tool in candidates:
            if not sliver_tool_bins.has_key(sliver_tool.site_id):
                sliver_tool_bins[sliver_tool.site_id] = [sliver_tool]
            else:
                sliver_tool_bins[sliver_tool.site_id].append(sliver_tool)

        for site_id in sliver_tool_bins:
            # Take a random sliver from the list.
            sliver_tool = random.choice(sliver_tool_bins[site_id])
            # Check if we already computed the distance of this site.
            if distances.has_key(sliver_tool.site_id):
                current_distance = distances[sliver_tool.site_id]
            else:
                current_distance = distance.distance(
                    query.latitude,
                    query.longitude,
                    sliver_tool.latitude,
                    sliver_tool.longitude)
                distances[sliver_tool.site_id] = current_distance

            sliver_tools.append(\
                sliver_tool_distance.SliverToolDistance(sliver_tool, \
                                                        current_distance))

        sliver_tools_sorted = sorted(sliver_tools, key=attrgetter('distance'))
        final_results = []
        for std in sliver_tools_sorted:
            final_results.append(std.sliver_tool)
        return final_results[:MAX_RESULTS]


class MetroResolver(ResolverBase):
    """Implements the metro policy."""

    def _get_candidates(self, query, address_family):
        # TODO(claudiu) Test whether the following query is better.
        # sites = model.Site.gql("WHERE metro = :metro", metro=query.metro)
        sites = model.Site.all().filter("metro =", query.metro).fetch(
            constants.MAX_FETCHED_RESULTS)
        logging.info(
            'Found %s results for metro %s.', len(sites), query.metro)
        if len(sites) == 0:
            logging.info('No results found for metro %s.', query.metro)
            return []

        site_id_list = []
        for site in sites:
            site_id_list.append(site.site_id)

        return self._get_candidates_from_sites(
            query, address_family, site_id_list)


class RandomResolver(ResolverBase):
    """Returns a server chosen randomly."""
    pass


class CountryResolver(ResolverBase):
    """Returns a server in a specified country."""

    def answer_query(self, query):
        """Returns a SliverTool in a specified country.

        Args:
            query: A LookupQuery instance.

        Returns:
            A SliverTool entity if available, None otherwise.
        """
        if query.user_defined_country is None:
            return None

        candidates = self.get_candidates(query)
        if len(candidates) == 0:
            logging.error('No results found for %s.', query.tool_id)
            return None

        country_candidates = []
        for candidate in candidates:
            if candidate.country == query.user_defined_country:
                country_candidates.append(candidate)

        if len(country_candidates) == 0:
            return None
        return [random.choice(country_candidates)]


def new_resolver(policy):
    if policy == message.POLICY_GEO:
        return GeoResolver()
    elif policy == message.POLICY_METRO:
        return MetroResolver()
    elif policy == message.POLICY_RANDOM:
        return RandomResolver()
    elif policy == message.POLICY_COUNTRY:
        return CountryResolver()
    elif policy == message.POLICY_GEO_OPTIONS:
        return GeoResolverWithOptions()
    elif policy == message.POLICY_ALL:
        return AllResolver()
    else:
        return RandomResolver()
