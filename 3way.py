#!/usr/bin/env python
# -*- coding: iso-8859-1 -*-

__doc__ = """Multiway association between astrometric catalogue. Use --help for usage.

Example: 3way.py --radius 10 --mag-radius 3 --prior-completeness 0.95 --mag GOODS:mag_H --mag IRAC:mag_irac1 cdfs4Ms_srclist_v3.fits :Pos_error CANDELS_irac1.fits 0.5 gs_short.fits 0.1 --out=out.fits
"""

import sys
import numpy
from numpy import log10, pi, exp, logical_and
import matplotlib.pyplot as plt
import pyfits
import argparse
import fastskymatch as match
import bayesdistance as bayesdist
import magnitudeweights

# set up program arguments

class HelpfulParser(argparse.ArgumentParser):
	def error(self, message):
		sys.stderr.write('error: %s\n' % message)
		self.print_help()
		sys.exit(2)

parser = HelpfulParser(description=__doc__,
	epilog="""Johannes Buchner (C) 2013 <jbuchner@mpe.mpg.de>""",
	formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--radius', default=10, type=float,
	help='exclusive search radius in arcsec for initial matching')

parser.add_argument('--mag-radius', default=3, type=float,
	help='search radius for magnitude histograms')

#nx/(1887*15e+18)
parser.add_argument('--prior-completeness', default=1, type=float,
	help='expected matching completeness of sources (prior)')

parser.add_argument('--mag', type=str, action='append', default=[],
	help="""name of <table>_<column> for magnitude biasing.
	Example: --mag GOODS:mag_H --mag IRAC:mag_irac1""")

parser.add_argument('--acceptable-prob', type=float, default=0.005,
	help='limit up to which secondary solutions are flagged')

parser.add_argument('--min-prob', type=float, default=0,
	help='lowest probability allowed in final catalogue. If 0, no trimming is performed.')

parser.add_argument('--out', help='output file name', required=True)

parser.add_argument('catalogues', type=str, nargs='+',
	help="""input catalogue fits files and position errors.
	Example: cdfs4Ms_srclist_v3.fits :Pos_error CANDELS_irac1.fits 0.5 gs_short.fits 0.1
	""")


# parsing arguments
args = parser.parse_args()

print '3way arguments:'

diff_secondary = args.acceptable_prob
outfile = args.out

filenames = args.catalogues[::2]
print '   catalogues: ', ', '.join(filenames)
pos_errors = args.catalogues[1::2]
print '   position errors/columns: ', ', '.join(pos_errors)

fits_tables = []
table_names = []
tables = []
source_densities = []
for fitsname in filenames:
	fits_table = pyfits.open(fitsname)[1]
	fits_tables.append(fits_table)
	table_name = fits_table.name
	table_names.append(table_name)
	table = fits_table.data
	tables.append(table)

	n = len(table)
	assert 'SKYAREA' in fits_table.header, "file %s, table %s does not have a field 'SKYAREA', which should contain the area of the catalogue in square degrees" % (fitsname, table_name)
	area = fits_table.header['SKYAREA'] # in square degrees
	area_total = (4 * pi * (180 / pi)**2)
	density = n / area * area_total
	print '     from catalogue %s, density is %e' % (table_name, density)
	source_densities.append(density)

prior = source_densities[0] * args.prior_completeness / numpy.product(source_densities)
print '   prior: %.2f * %2.2f%% / %e = %e' % (source_densities[0], args.prior_completeness * 100, numpy.product(source_densities), prior)

min_prob = args.min_prob

match_radius = args.radius / 60. / 60 # in degrees
mag_radius = args.mag_radius # in arc sec

magnitude_columns = args.mag
print '   magnitude columns: ', ', '.join(magnitude_columns)

# first match input catalogues, compute possible combinations in match_radius
results, columns = match.match_multiple(tables, table_names, match_radius)
table = pyfits.new_table(pyfits.ColDefs(columns)).data


# find magnitude biasing functions
biases = {}
for mag in magnitude_columns:
	table_name, col_name = mag.split(':', 1)
	assert table_name in table_names, 'table name specified for magnitude (%s) unknown. Known tables: %s' % (table_name, ', '.join(table_names))
	ti = table_names.index(table_name)
	col_names = tables[ti].dtype.names
	assert col_name in col_names, 'column name specified for magnitude (%s) unknown. Known columns in table %s: %s' % (mag, table_name, ', '.join(col_names))
	ci = col_names.index(col_name)
	
	# get magnitudes of all
	mag_all = tables[ti][col_name]
	# mark -99 as undefined
	mag_all[mag_all == -99] = numpy.nan
	
	# get magnitudes of selected
	mask_all = -numpy.logical_or(numpy.isnan(mag_all), numpy.isinf(mag_all))
	
	rows = list(set(results[table_name][table['Separation_max'] < mag_radius]))
	mag_sel = mag_all[rows]
	mask_radius = table['Separation_max'] < mag_radius
	mask_sel = -numpy.logical_or(numpy.isnan(mag_sel), numpy.isinf(mag_sel))
	col = "%s_%s" % (table_name, col_name)
	print 'magnitude histogramming: %d matches in magnitude radius. rows used from %s: %d (%d valid)' % (mask_radius.sum(), col, len(mag_sel), mask_sel.sum())
	
	# make function fitting to ratio shape
	bins, hist_sel, hist_all = magnitudeweights.adaptive_histograms(mag_all[mask_all], mag_sel[mask_sel])
	func = magnitudeweights.fitfunc_histogram(bins, hist_sel, hist_all)
	magnitudeweights.plot_fit(bins, hist_sel, hist_all, func, mag)
	weights = func(table[col])
	# undefined magnitudes do not contribute
	weights[numpy.isnan(weights)] = 0
	biases[col] = weights

# get the separation and error columns for the bayesian weighting
errors    = []
for table_name, pos_error in zip(table_names, pos_errors):
	if pos_error[0] == ':':
		# get column
		k = "%s_%s" % (table_name, pos_error[1:])
		assert k in table.dtype.names, 'ERROR: Position error column for %s not in table %s. Have columns: %s' % (k, table_name, ', '.join(table.dtype.names))
		print 'using column', (table[k].min(), table[k].max())
		errors.append(table[k])
	else:
		errors.append(float(pos_error[1:]) * numpy.ones(len(table)))

separations = []
for ti, a in enumerate(table_names):
	row = []
	for tj, b in enumerate(table_names):
		if ti < tj:
			k = 'Separation_%s_%s' % (b, a)
			assert k in table.dtype.names, 'ERROR: Separation column for %s not in merged table. Have columns: %s' % (k, ', '.join(table.dtype.names))
			row.append(table[k])
		else:
			row.append(numpy.ones(len(table)) * numpy.nan)
	separations.append(row)

# compute n-way position evidence

log_bf = bayesdist.log_bf(separations, errors)

# add the additional columns
columns.append(pyfits.Column(name='bf', format='E', array=log_bf))
columns.append(pyfits.Column(name='bfpost', format='E', array=bayesdist.posterior(prior, log_bf)))

# add the bias columns
for col, weights in biases.iteritems():
	columns.append(pyfits.Column(name='bias_%s' % col, format='E', array=10**weights))


# add the posterior column
total = log_bf + sum(biases.values())
post = bayesdist.posterior(prior, total)
columns.append(pyfits.Column(name='post', format='E', array=post))

# flagging of solutions. Go through groups by primary id (IDs in first catalogue)
index = numpy.zeros_like(post)

primary_id_key = match.get_tablekeys(tables[0], 'ID')
print 'grouping by %s from %s' % (primary_id_key, table_names[0])
primary_id_key = '%s_%s' % (table_names[0], primary_id_key)

primary_ids = sorted(set(table[primary_id_key]))

for primary_id in primary_ids:
	# group
	mask = table[primary_id_key] == primary_id
	group_posterior = post[mask]
	best_index = group_posterior.argmax()
	best_val = group_posterior[best_index]
	
	# flag second best
	mask2 = logical_and(mask, best_val - post < diff_secondary)
	# ignore very poor solutions
	mask2 = logical_and(mask2, post > 0.1)
	index[mask2] = 2
	# flag best
	mask1 = logical_and(mask, best_val == post)
	index[mask1] = 1

# add the flagging column
columns.append(pyfits.Column(name='match_flag', format='I', array=index))

# cut away poor posteriors if requested
if min_prob > 0:
	mask = -(post < min_prob)
	print 'cutting away %d (below minimum)' % mask.sum()

	for c in columns:
		c.array = c.array[mask]

# write out fits file
tbhdu = pyfits.new_table(pyfits.ColDefs(columns))
hdulist = match.wraptable2fits(tbhdu, 'MULTIMATCH')
hdulist[0].header.update('METHOD', 'multi-way matching')
hdulist[0].header.update('INPUT', ', '.join(filenames))
hdulist[0].header.update('TABLES', ', '.join(table_names))
hdulist[0].header.update('BIASING', ', '.join(biases.keys()))
for k, v in args.__dict__.iteritems():
	hdulist[0].header.add_comment("argument %s: %s" % (k, v))
hdulist.writeto(outfile, clobber=True)

print 'wrote %s (%d rows, %d columns)' % (outfile, len(tbhdu.data), len(columns))




