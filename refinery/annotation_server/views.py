from decimal import *
import logging
import simplejson

from django.db import connection
from django.db.models import Q
from django.http import HttpResponse

from annotation_server.models import *
from annotation_server.utils import *


logger = logging.getLogger(__name__)


def search_genes(request, genome, search_string):
    """Function for searching basic gene table currently:
    Ensembl (EnsGene table from UCSC genome browser)
    """
    logger.debug("annotation_server.search_genes called for genome: "
                 "%s search: %s",
                 genome, search_string)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_EnsGene")
        curr_vals = current_table.objects.filter(
            Q(name__contains=search_string) | Q(name2__contains=search_string)
        ).values('name', 'chrom', 'strand', 'txStart', 'txEnd', 'cdsStart',
                 'cdsEnd', 'exonCount', 'exonStarts', 'exonEnds')

        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)

    # Postbio query
    # cursor = connection.cursor()
    # query = """Select a.name, a.symbol, a.synonyms,
    # <b.region as start, #>b.region as end, b.chrom FROM (SELECT name, symbol,
    # synonyms from dm3.flybase2004xref where symbol ilike '%s') a JOIN
    # (SELECT f.name, f.region, f.seq_id, s.name as chrom FROM dm3.flybase f
    # JOIN dm3.sequence s ON f.seq_id = s.id where s.name = 'chr2L' OR
    # s.name = 'chr2R' OR s.name = 'chr3L' OR s.name = 'chr3R' OR
    # s.name = 'chr4' OR s.name ='chrX' )  b ON a.name = b.name """ %
    # (search_string)
    # cursor.execute(query)

    return HttpResponse(cursor_to_json(cursor), 'application/javascript')


def search_extended_genes(request, genome, search_string):
    """Function for searching extended gene tables currently:
    GenCode (hg19), Flybase (dm3), or Wormbase (ce10)
    """
    logger.debug("annotation_server.search_extended_genes called for genome: "
                 "%s search: %s", genome, search_string)


def get_sequence(request, genome, chrom, start, end):
    """returns sequence for a specified chromosome start and end"""
    logger.debug("annotation_server.get_sequence called for genome: "
                 "%s chrom: %s", genome, chrom)
    offset = int(end) - int(start)
    # NO SUBSTRING METHOD USING DJANGO ORM
    if genome in SUPPORTED_GENOMES:
        cursor = connection.cursor()
        db_table = 'annotation_server_dm3_sequence'
        query = ("select name as chrom, substr(seq, %s, %s) as seq "
                 "from annotation_server_%s_sequence where name = '%s'")\
            .format(start, offset, genome, chrom)
        cursor.execute(query)
        return HttpResponse(cursor_to_json(cursor), 'application/javascript')
    else:
        return HttpResponse(status=400)


def get_length(request, genome):
    """Returns all chromosome lengths depending on genome i.e. dm3, hg18, etc
    """
    logger.debug("annotation_server.get_length called for genome: %s", genome)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_ChromInfo")
        data = ValuesQuerySetToDict(
            current_table.objects.values('chrom', 'size'))
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def get_chrom_length(request, genome, chrom):
    """returns the length of a specified chromosome"""
    logger.debug("annotation_server.get_chrom_length called for genome: "
                 "%s chromosome: %s", genome, chrom)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_ChromInfo")
        curr_vals = current_table.objects.filter(chrom__iexact=chrom).values(
            'chrom', 'size')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)

    # TODO: return genome lengths according to chrom order i.e. 1,2,3 etc.


def get_cytoband(request, genome, chrom):
    """returns the length of a specified chromosome"""
    logger.debug("annotation_server.get_cytoband called for genome: "
                 "%s chromosome: %s", genome, chrom)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_CytoBand")
        curr_vals = current_table.objects.filter(chrom__iexact=chrom).values(
            'chrom', 'chromStart', 'chromEnd', 'name', 'gieStain')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def get_genes(request, genome, chrom, start, end):
    """gets a list of genes within a range i.e. gene start, cds, gene symbol"""
    logger.debug("annotation_server.get_genes called for genome: "
                 "%s chromosome: %s", genome, chrom)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_EnsGene")
        curr_vals = current_table.objects.filter(
            Q(chrom__iexact=chrom),
            Q(cdsStart__range=(start, end)) | Q(cdsEnd__range=(start, end))
        ).values('name', 'chrom', 'strand', 'txStart', 'txEnd', 'cdsStart',
                 'cdsEnd', 'exonCount', 'exonStarts', 'exonEnds')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def get_gc(request, genome, chrom, start, end):
    """gets GC content within a range i.e. gene start, cds, gene symbol"""
    logger.debug("annotation_server.get_gc called for genome: "
                 "%s chromosome: %s:%s-%s", genome, chrom, start, end)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_GC")
        curr_vals = current_table.objects.filter(
            Q(chrom__iexact=chrom),
            Q(position__range=(start, end)),
        ).values('chrom', 'position', 'value')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def get_maptheo(request, genome, chrom, start, end):
    """gets Theoretical Mappability annotation track within a range
    i.e. gene start, cds, gene symbol
    """
    logger.debug("annotation_server.get_maptheo called for genome: "
                 "%s chromosome: %s:%s-%s", genome, chrom, start, end)

    if genome in MAPPABILITY_THEORETICAL:
        current_table = eval(genome + "_MappabilityTheoretical")
        curr_vals = current_table.objects.filter(
            Q(chrom__iexact=chrom),
            Q(chromStart__range=(start, end)) | Q(chromEnd__range=(start, end))
        ).values('chrom', 'chromStart', 'chromEnd')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def get_mapemp(request, genome, chrom, start, end):
    """gets Theoretical Mappability annotation track within a range
    """
    logger.debug("annotation_server.get_mapemp called for genome: "
                 "%s chromosome: %s:%s-%s", genome, chrom, start, end)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_MappabilityEmpirial")
        curr_vals = current_table.objects.filter(
            Q(chrom__iexact=chrom),
            Q(chromStart__range=(start, end)) | Q(chromEnd__range=(start, end))
        ).values('chrom', 'chromStart', 'chromEnd')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def get_conservation(request, genome, chrom, start, end):
    """gets Conservation annotation scores within a range"""
    logger.debug("annotation_server.get_conservation called for genome: "
                 "%s chromosome: %s:%s-%s", genome, chrom, start, end)

    if genome in SUPPORTED_GENOMES:
        current_table = eval(genome + "_Conservation")
        curr_vals = current_table.objects.filter(
            Q(chrom__iexact=chrom),
            Q(position__range=(start, end)),
        ).values('chrom', 'position', 'value')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def get_gapregion(request, genome, chrom, start, end):
    """gets Conservation annotation scores within a range
    """
    logger.debug("annotation_server.get_gapregion called for genome: "
                 "%s chromosome: %s:%s-%s", genome, chrom, start, end)

    if genome in GAP_REGIONS:
        current_table = eval(genome + "_GapRegions")
        curr_vals = current_table.objects.filter(
            Q(chrom__iexact=chrom),
            Q(chromStart__gte=start),
            Q(chromStart__range=(start, end)) | Q(chromEnd__range=(start, end))
        ).values('bin', 'chromStart', 'chromEnd', 'ix', 'n', 'size', 'type',
                 'bridge')
        data = ValuesQuerySetToDict(curr_vals)
        return HttpResponse(data, 'application/json')
    else:
        return HttpResponse(status=400)


def cursor_to_json(cursor_in):
    cols = [x[0] for x in cursor_in.description]
    out = []
    for r in cursor_in.fetchall():
        row = {}
        for prop, val in zip(cols, r):
            if isinstance(val, Decimal):
                val = float(val)
            row[prop] = val
        out.append(row)
    return simplejson.dumps(out)


def ValuesQuerySetToDict(vqs):
    """Helper function converts ValuesQuerySet to Dictionary
    simpler way of dumping data to json without extraneous models and pk
    Based on http://djangosnippets.org/snippets/2454/
    """
    ret = [item for item in vqs]
    return simplejson.dumps(ret, indent=4)
