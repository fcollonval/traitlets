"""A simple configuration system."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

import argparse
import copy
import os
import re
import sys
import json
import warnings

from ..utils import cast_unicode

from ipython_genutils.path import filefind
from ipython_genutils.encoding import DEFAULT_ENCODING
from traitlets.traitlets import (
    HasTraits, Container, List, Dict, Any, Undefined,
)

#-----------------------------------------------------------------------------
# Exceptions
#-----------------------------------------------------------------------------


class ConfigError(Exception):
    pass

class ConfigLoaderError(ConfigError):
    pass

class ConfigFileNotFound(ConfigError):
    pass

class ArgumentError(ConfigLoaderError):
    pass

#-----------------------------------------------------------------------------
# Argparse fix
#-----------------------------------------------------------------------------

# Unfortunately argparse by default prints help messages to stderr instead of
# stdout.  This makes it annoying to capture long help screens at the command
# line, since one must know how to pipe stderr, which many users don't know how
# to do.  So we override the print_help method with one that defaults to
# stdout and use our class instead.


class _Sentinel:
    def __repr__(self):
        return "<Sentinel deprecated>"

    def __str__(self):
        return "<deprecated>"


_deprecated = _Sentinel()


class ArgumentParser(argparse.ArgumentParser):
    """Simple argparse subclass that prints help to stdout by default."""

    def print_help(self, file=None):
        if file is None:
            file = sys.stdout
        return super(ArgumentParser, self).print_help(file)

    print_help.__doc__ = argparse.ArgumentParser.print_help.__doc__

#-----------------------------------------------------------------------------
# Config class for holding config information
#-----------------------------------------------------------------------------

def execfile(fname, glob):
    with open(fname, 'rb') as f:
        exec(compile(f.read(), fname, 'exec'), glob, glob)

class LazyConfigValue(HasTraits):
    """Proxy object for exposing methods on configurable containers

    These methods allow appending/extending/updating
    to add to non-empty defaults instead of clobbering them.

    Exposes:

    - append, extend, insert on lists
    - update on dicts
    - update, add on sets
    """

    _value = None

    # list methods
    _extend = List()
    _prepend = List()
    _inserts = List()

    def append(self, obj):
        """Append an item to a List"""
        self._extend.append(obj)

    def extend(self, other):
        """Extend a list"""
        self._extend.extend(other)

    def prepend(self, other):
        """like list.extend, but for the front"""
        self._prepend[:0] = other


    def merge_into(self, other):
        """
        Merge with another earlier LazyConfigValue or an earlier container.
        This is useful when having global system-wide configuration files.

        Self is expected to have higher precedence.

        Parameters
        ----------
        other : LazyConfigValue or container

        Returns
        -------
        LazyConfigValue
            if ``other`` is also lazy, a reified container otherwise.
        """
        if isinstance(other, LazyConfigValue):
            other._extend.extend(self._extend)
            self._extend = other._extend

            self._prepend.extend(other._prepend)

            other._inserts.extend(self._inserts)
            self._inserts = other._inserts

            if self._update:
                other.update(self._update)
                self._update = other._update
            return self
        else:
            # other is a container, reify now.
            return self.get_value(other)

    def insert(self, index, other):
        if not isinstance(index, int):
            raise TypeError("An integer is required")
        self._inserts.append((index, other))

    # dict methods
    # update is used for both dict and set
    _update = Any()

    def update(self, other):
        """Update either a set or dict"""
        if self._update is None:
            if isinstance(other, dict):
                self._update = {}
            else:
                self._update = set()
        self._update.update(other)

    # set methods
    def add(self, obj):
        """Add an item to a set"""
        self.update({obj})

    def get_value(self, initial):
        """construct the value from the initial one

        after applying any insert / extend / update changes
        """
        if self._value is not None:
            return self._value
        value = copy.deepcopy(initial)
        if isinstance(value, list):
            for idx, obj in self._inserts:
                value.insert(idx, obj)
            value[:0] = self._prepend
            value.extend(self._extend)

        elif isinstance(value, dict):
            if self._update:
                value.update(self._update)
        elif isinstance(value, set):
            if self._update:
                value.update(self._update)
        self._value = value
        return value

    def to_dict(self):
        """return JSONable dict form of my data

        Currently update as dict or set, extend, prepend as lists, and inserts as list of tuples.
        """
        d = {}
        if self._update:
            d['update'] = self._update
        if self._extend:
            d['extend'] = self._extend
        if self._prepend:
            d['prepend'] = self._prepend
        elif self._inserts:
            d['inserts'] = self._inserts
        return d

    def __repr__(self):
        if self._value is not None:
            return "<%s value=%r>" % (self.__class__.__name__, self._value)
        else:
            return "<%s %r>" % (self.__class__.__name__, self.to_dict())


def _is_section_key(key):
    """Is a Config key a section name (does it start with a capital)?"""
    if key and key[0].upper()==key[0] and not key.startswith('_'):
        return True
    else:
        return False


class Config(dict):
    """An attribute-based dict that can do smart merges.

    Accessing a field on a config object for the first time populates the key
    with either a nested Config object for keys starting with capitals
    or :class:`.LazyConfigValue` for lowercase keys,
    allowing quick assignments such as::

        c = Config()
        c.Class.int_trait = 5
        c.Class.list_trait.append("x")

    """

    def __init__(self, *args, **kwds):
        dict.__init__(self, *args, **kwds)
        self._ensure_subconfig()

    def _ensure_subconfig(self):
        """ensure that sub-dicts that should be Config objects are

        casts dicts that are under section keys to Config objects,
        which is necessary for constructing Config objects from dict literals.
        """
        for key in self:
            obj = self[key]
            if _is_section_key(key) \
                    and isinstance(obj, dict) \
                    and not isinstance(obj, Config):
                setattr(self, key, Config(obj))

    def _merge(self, other):
        """deprecated alias, use Config.merge()"""
        self.merge(other)

    def merge(self, other):
        """merge another config object into this one"""
        to_update = {}
        for k, v in other.items():
            if k not in self:
                to_update[k] = v
            else: # I have this key
                if isinstance(v, Config) and isinstance(self[k], Config):
                    # Recursively merge common sub Configs
                    self[k].merge(v)
                elif isinstance(v, LazyConfigValue):
                    self[k] = v.merge_into(self[k])
                else:
                    # Plain updates for non-Configs
                    to_update[k] = v

        self.update(to_update)

    def collisions(self, other):
        """Check for collisions between two config objects.

        Returns a dict of the form {"Class": {"trait": "collision message"}}`,
        indicating which values have been ignored.

        An empty dict indicates no collisions.
        """
        collisions = {}
        for section in self:
            if section not in other:
                continue
            mine = self[section]
            theirs = other[section]
            for key in mine:
                if key in theirs and mine[key] != theirs[key]:
                    collisions.setdefault(section, {})
                    collisions[section][key] = "%r ignored, using %r" % (mine[key], theirs[key])
        return collisions

    def __contains__(self, key):
        # allow nested contains of the form `"Section.key" in config`
        if '.' in key:
            first, remainder = key.split('.', 1)
            if first not in self:
                return False
            return remainder in self[first]

        return super(Config, self).__contains__(key)

    # .has_key is deprecated for dictionaries.
    has_key = __contains__

    def _has_section(self, key):
        return _is_section_key(key) and key in self

    def copy(self):
        return type(self)(dict.copy(self))

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        new_config = type(self)()
        for key, value in self.items():
            if isinstance(value, (Config, LazyConfigValue)):
                # deep copy config objects
                value = copy.deepcopy(value, memo)
            elif type(value) in {dict, list, set, tuple}:
                # shallow copy plain container traits
                value = copy.copy(value)
            new_config[key] = value
        return new_config

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            if _is_section_key(key):
                c = Config()
                dict.__setitem__(self, key, c)
                return c
            elif not key.startswith('_'):
                # undefined, create lazy value, used for container methods
                v = LazyConfigValue()
                dict.__setitem__(self, key, v)
                return v
            else:
                raise KeyError

    def __setitem__(self, key, value):
        if _is_section_key(key):
            if not isinstance(value, Config):
                raise ValueError('values whose keys begin with an uppercase '
                                 'char must be Config instances: %r, %r' % (key, value))
        dict.__setitem__(self, key, value)

    def __getattr__(self, key):
        if key.startswith('__'):
            return dict.__getattr__(self, key)
        try:
            return self.__getitem__(key)
        except KeyError as e:
            raise AttributeError(e)

    def __setattr__(self, key, value):
        if key.startswith('__'):
            return dict.__setattr__(self, key, value)
        try:
            self.__setitem__(key, value)
        except KeyError as e:
            raise AttributeError(e)

    def __delattr__(self, key):
        if key.startswith('__'):
            return dict.__delattr__(self, key)
        try:
            dict.__delitem__(self, key)
        except KeyError as e:
            raise AttributeError(e)


class DeferredConfig:
    """Class for deferred-evaluation of config from CLI"""
    pass

    def get_value(self, trait):
        raise NotImplementedError("Implement in subclasses")

    def _super_repr(self):
        # explicitly call super on direct parent
        return super(self.__class__, self).__repr__()


class DeferredConfigString(str, DeferredConfig):
    """Config value for loading config from a string

    Interpretation is deferred until it is loaded into the trait.

    Subclass of str for backward compatibility.

    This class is only used for values that are not listed
    in the configurable classes.

    When config is loaded, `trait.from_string` will be used.

    If an error is raised in `.from_string`,
    the original string is returned.

    .. versionadded:: 5.0
    """
    def get_value(self, trait):
        """Get the value stored in this string"""
        s = str(self)
        try:
            return trait.from_string(s)
        except Exception:
            # exception casting from string,
            # let the original string lie.
            # this will raise a more informative error when config is loaded.
            return s

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self._super_repr())


class DeferredConfigList(list, DeferredConfig):
    """Config value for loading config from a list of strings

    Interpretation is deferred until it is loaded into the trait.

    This class is only used for values that are not listed
    in the configurable classes.

    When config is loaded, `trait.from_string_list` will be used.

    If an error is raised in `.from_string_list`,
    the original string list is returned.

    .. versionadded:: 5.0
    """
    def get_value(self, trait):
        """Get the value stored in this string"""
        if hasattr(trait, "from_string_list"):
            src = list(self)
            cast = trait.from_string_list
        else:
            # only allow one item
            if len(self) > 1:
                raise ValueError(f"{trait.name} only accepts one value, got {len(self)}: {list(self)}")
            src = self[0]
            cast = trait.from_string

        try:
            return cast(src)
        except Exception:
            # exception casting from string,
            # let the original value lie.
            # this will raise a more informative error when config is loaded.
            return src

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self._super_repr())


#-----------------------------------------------------------------------------
# Config loading classes
#-----------------------------------------------------------------------------


class ConfigLoader(object):
    """A object for loading configurations from just about anywhere.

    The resulting configuration is packaged as a :class:`Config`.

    Notes
    -----
    A :class:`ConfigLoader` does one thing: load a config from a source
    (file, command line arguments) and returns the data as a :class:`Config` object.
    There are lots of things that :class:`ConfigLoader` does not do.  It does
    not implement complex logic for finding config files.  It does not handle
    default values or merge multiple configs.  These things need to be
    handled elsewhere.
    """

    def _log_default(self):
        from traitlets.log import get_logger
        return get_logger()

    def __init__(self, log=None):
        """A base class for config loaders.

        log : instance of :class:`logging.Logger` to use.
              By default logger of :meth:`traitlets.config.application.Application.instance()`
              will be used

        Examples
        --------
        >>> cl = ConfigLoader()
        >>> config = cl.load_config()
        >>> config
        {}
        """
        self.clear()
        if log is None:
            self.log = self._log_default()
            self.log.debug('Using default logger')
        else:
            self.log = log

    def clear(self):
        self.config = Config()

    def load_config(self):
        """Load a config from somewhere, return a :class:`Config` instance.

        Usually, this will cause self.config to be set and then returned.
        However, in most cases, :meth:`ConfigLoader.clear` should be called
        to erase any previous state.
        """
        self.clear()
        return self.config


class FileConfigLoader(ConfigLoader):
    """A base class for file based configurations.

    As we add more file based config loaders, the common logic should go
    here.
    """

    def __init__(self, filename, path=None, **kw):
        """Build a config loader for a filename and path.

        Parameters
        ----------
        filename : str
            The file name of the config file.
        path : str, list, tuple
            The path to search for the config file on, or a sequence of
            paths to try in order.
        """
        super(FileConfigLoader, self).__init__(**kw)
        self.filename = filename
        self.path = path
        self.full_filename = ''

    def _find_file(self):
        """Try to find the file by searching the paths."""
        self.full_filename = filefind(self.filename, self.path)

class JSONFileConfigLoader(FileConfigLoader):
    """A JSON file loader for config

    Can also act as a context manager that rewrite the configuration file to disk on exit.

    Example::

        with JSONFileConfigLoader('myapp.json','/home/jupyter/configurations/') as c:
            c.MyNewConfigurable.new_value = 'Updated'

    """

    def load_config(self):
        """Load the config from a file and return it as a Config object."""
        self.clear()
        try:
            self._find_file()
        except IOError as e:
            raise ConfigFileNotFound(str(e))
        dct = self._read_file_as_dict()
        self.config = self._convert_to_config(dct)
        return self.config

    def _read_file_as_dict(self):
        with open(self.full_filename) as f:
            return json.load(f)

    def _convert_to_config(self, dictionary):
        if 'version' in dictionary:
            version = dictionary.pop('version')
        else:
            version = 1

        if version == 1:
            return Config(dictionary)
        else:
            raise ValueError('Unknown version of JSON config file: {version}'.format(version=version))

    def __enter__(self):
        self.load_config()
        return self.config

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Exit the context manager but do not handle any errors.

        In case of any error, we do not want to write the potentially broken
        configuration to disk.
        """
        self.config.version = 1
        json_config = json.dumps(self.config, indent=2)
        with open(self.full_filename, 'w') as f:
            f.write(json_config)



class PyFileConfigLoader(FileConfigLoader):
    """A config loader for pure python files.

    This is responsible for locating a Python config file by filename and
    path, then executing it to construct a Config object.
    """

    def load_config(self):
        """Load the config from a file and return it as a Config object."""
        self.clear()
        try:
            self._find_file()
        except IOError as e:
            raise ConfigFileNotFound(str(e))
        self._read_file_as_dict()
        return self.config

    def load_subconfig(self, fname, path=None):
        """Injected into config file namespace as load_subconfig"""
        if path is None:
            path = self.path

        loader = self.__class__(fname, path)
        try:
            sub_config = loader.load_config()
        except ConfigFileNotFound:
            # Pass silently if the sub config is not there,
            # treat it as an empty config file.
            pass
        else:
            self.config.merge(sub_config)

    def _read_file_as_dict(self):
        """Load the config file into self.config, with recursive loading."""
        def get_config():
            """Unnecessary now, but a deprecation warning is more trouble than it's worth."""
            return self.config

        namespace = dict(
            c=self.config,
            load_subconfig=self.load_subconfig,
            get_config=get_config,
            __file__=self.full_filename,
        )
        conf_filename = self.full_filename
        with open(conf_filename, 'rb') as f:
            exec(compile(f.read(), conf_filename, 'exec'), namespace, namespace)


class CommandLineConfigLoader(ConfigLoader):
    """A config loader for command line arguments.

    As we add more command line based loaders, the common logic should go
    here.
    """

    def _exec_config_str(self, lhs, rhs, trait=None):
        """execute self.config.<lhs> = <rhs>

        * expands ~ with expanduser
        * interprets value with trait if available
        """
        value = rhs
        if isinstance(value, DeferredConfig):
            if trait:
                # trait available, reify config immediately
                value = value.get_value(trait)
            elif isinstance(rhs, DeferredConfigList) and len(rhs) == 1:
                # single item, make it a deferred str
                value = DeferredConfigString(os.path.expanduser(rhs[0]))
        else:
            if trait:
                value = trait.from_string(value)
            else:
                value = DeferredConfigString(value)

        *path, key = lhs.split(".")
        section = self.config
        for part in path:
            section = section[part]
        section[key] = value
        return

    def _load_flag(self, cfg):
        """update self.config from a flag, which can be a dict or Config"""
        if isinstance(cfg, (dict, Config)):
            # don't clobber whole config sections, update
            # each section from config:
            for sec, c in cfg.items():
                self.config[sec].update(c)
        else:
            raise TypeError("Invalid flag: %r" % cfg)

# match --Class.trait keys for argparse
# matches:
# --Class.trait
# --x
# -x

class_trait_opt_pattern = re.compile(r'^\-?\-[A-Za-z][\w]*(\.[\w]+)*$')

_DOT_REPLACEMENT = "__DOT__"
_DASH_REPLACEMENT = "__DASH__"


class _KVAction(argparse.Action):
    """Custom argparse action for handling --Class.trait=x

    Always
    """
    def __call__(self, parser, namespace, values, option_string=None):
        if isinstance(values, str):
            values = [values]
        values = ["-" if v is _DASH_REPLACEMENT else v for v in values]
        items = getattr(namespace, self.dest, None)
        if items is None:
            items = DeferredConfigList()
        else:
            items = DeferredConfigList(items)
        items.extend(values)
        setattr(namespace, self.dest, items)


class _DefaultOptionDict(dict):
    """Like the default options dict

    but acts as if all --Class.trait options are predefined
    """
    def _add_kv_action(self, key):
        self[key] = _KVAction(
            option_strings=[key],
            dest=key.lstrip("-").replace(".", _DOT_REPLACEMENT),
            # use metavar for display purposes
            metavar=key.lstrip("-"),
        )

    def __contains__(self, key):
        if '=' in key:
            return False
        if super().__contains__(key):
            return True

        if key.startswith("-") and class_trait_opt_pattern.match(key):
            self._add_kv_action(key)
            return True
        return False

    def __getitem__(self, key):
        if key in self:
            return super().__getitem__(key)
        else:
            raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class _KVArgParser(argparse.ArgumentParser):
    """subclass of ArgumentParser where any --Class.trait option is implicitly defined"""
    def parse_known_args(self, args=None, namespace=None):
        # must be done immediately prior to parsing because if we do it in init,
        # registration of explicit actions via parser.add_option will fail during setup
        for container in (self, self._optionals):
            container._option_string_actions = _DefaultOptionDict(
                container._option_string_actions)
        return super().parse_known_args(args, namespace)


class ArgParseConfigLoader(CommandLineConfigLoader):
    """A loader that uses the argparse module to load from the command line."""

    parser_class = ArgumentParser

    def __init__(self, argv=None, aliases=None, flags=None, log=None, classes=(),
                 *parser_args, **parser_kw):
        """Create a config loader for use with argparse.

        Parameters
        ----------
        classes : optional, list
            The classes to scan for *container* config-traits and decide
            for their "multiplicity" when adding them as *argparse* arguments.
        argv : optional, list
            If given, used to read command-line arguments from, otherwise
            sys.argv[1:] is used.
        *parser_args : tuple
            A tuple of positional arguments that will be passed to the
            constructor of :class:`argparse.ArgumentParser`.
        **parser_kw : dict
            A tuple of keyword arguments that will be passed to the
            constructor of :class:`argparse.ArgumentParser`.
        aliases : dict of str to str
            Dict of aliases to full traitlests names for CLI parsing
        flags : dict of str to str
            Dict of flags to full traitlests names for CLI parsing
        log
            Passed to `ConfigLoader`

        Returns
        -------
        config : Config
            The resulting Config object.
        """
        super(CommandLineConfigLoader, self).__init__(log=log)
        self.clear()
        if argv is None:
            argv = sys.argv[1:]
        self.argv = argv
        self.aliases = aliases or {}
        self.flags = flags or {}
        self.classes = classes

        self.parser_args = parser_args
        self.version = parser_kw.pop("version", None)
        kwargs = dict(argument_default=argparse.SUPPRESS)
        kwargs.update(parser_kw)
        self.parser_kw = kwargs

    def load_config(self, argv=None, aliases=None, flags=_deprecated, classes=None):
        """Parse command line arguments and return as a Config object.

        Parameters
        ----------
        argv : optional, list
            If given, a list with the structure of sys.argv[1:] to parse
            arguments from. If not given, the instance's self.argv attribute
            (given at construction time) is used.
        flags
            Deprecated in traitlets 5.0, instanciate the config loader with the flags.

        """

        if flags is not _deprecated:
            warnings.warn(
                "The `flag` argument to load_config is deprecated since Traitlets "
                f"5.0 and will be ignored, pass flags the `{type(self)}` constructor.",
                DeprecationWarning,
                stacklevel=2,
            )

        self.clear()
        if argv is None:
            argv = self.argv
        if aliases is not None:
            self.aliases = aliases
        if classes is not None:
            self.classes = classes
        self._create_parser()
        self._parse_args(argv)
        self._convert_to_config()
        return self.config

    def get_extra_args(self):
        if hasattr(self, 'extra_args'):
            return self.extra_args
        else:
            return []

    def _create_parser(self):
        self.parser = self.parser_class(*self.parser_args, **self.parser_kw)
        self._add_arguments(self.aliases, self.flags, self.classes)

    def _add_arguments(self, aliases, flags, classes):
        raise NotImplementedError("subclasses must implement _add_arguments")

    def _parse_args(self, args):
        """self.parser->self.parsed_data"""
        # decode sys.argv to support unicode command-line options
        enc = DEFAULT_ENCODING
        uargs = [cast_unicode(a, enc) for a in args]

        unpacked_aliases = {}
        if self.aliases:
            unpacked_aliases = {}
            for alias, alias_target in self.aliases.items():
                if alias in self.flags:
                    continue
                if not isinstance(alias, tuple):
                    short_alias, alias = alias, None
                else:
                    short_alias, alias = alias
                for al in (short_alias, alias):
                    if al is None:
                        continue
                    if len(al) == 1:
                        unpacked_aliases["-" + al] = "--" + alias_target
                    unpacked_aliases["--" + al] = "--" + alias_target

        def _replace(arg):
            if arg == "-":
                return _DASH_REPLACEMENT
            for k, v in unpacked_aliases.items():
                if arg == k:
                    return v
                if arg.startswith(k + "="):
                    return v + "=" + arg[len(k) + 1:]
            return arg

        if '--' in uargs:
            idx = uargs.index('--')
            extra_args = uargs[idx+1:]
            to_parse = uargs[:idx]
        else:
            extra_args = []
            to_parse = uargs
        to_parse = [_replace(a) for a in to_parse]

        self.parsed_data = self.parser.parse_args(to_parse)
        self.extra_args = extra_args

    def _convert_to_config(self):
        """self.parsed_data->self.config"""
        for k, v in vars(self.parsed_data).items():
            *path, key = k.split(".")
            section = self.config
            for p in path:
                section = section[p]
            setattr(section, key, v)


class _FlagAction(argparse.Action):
    """ArgParse action to handle a flag"""
    def __init__(self, *args, **kwargs):
        self.flag = kwargs.pop('flag')
        self.alias = kwargs.pop('alias', None)
        kwargs['const'] = Undefined
        if not self.alias:
            kwargs['nargs'] = 0
        super(_FlagAction, self).__init__(*args, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        if self.nargs == 0 or values is Undefined:
            namespace._flags.append(self.flag)
        else:
            setattr(namespace, self.alias, values)


class KVArgParseConfigLoader(ArgParseConfigLoader):
    """A config loader that loads aliases and flags with argparse,

    as well as arbitrary --Class.trait value
    """

    parser_class = _KVArgParser

    def _add_arguments(self, aliases, flags, classes):
        alias_flags = {}
        paa = self.parser.add_argument
        self.parser.set_defaults(_flags=[])
        paa("extra_args", nargs="*")

        ## An index of all container traits collected::
        #
        #     { <traitname>: (<trait>, <argparse-kwds>) }
        #
        #  Used to add the correct type into the `config` tree.
        #  Used also for aliases, not to re-collect them.
        self.argparse_traits = argparse_traits = {}
        for cls in classes:
            for traitname, trait in cls.class_traits(config=True).items():
                argname = '%s.%s' % (cls.__name__, traitname)
                argparse_kwds = {'type': str}
                if isinstance(trait, (Container, Dict)):
                    multiplicity = trait.metadata.get('multiplicity', 'append')
                    if multiplicity == 'append':
                        argparse_kwds['action'] = multiplicity
                    else:
                        argparse_kwds['nargs'] = multiplicity
                argparse_traits[argname] = (trait, argparse_kwds)

        for keys, (value, _) in flags.items():
            if not isinstance(keys, tuple):
                keys = (keys,)
            for key in keys:
                if key in aliases:
                    alias_flags[aliases[key]] = value
                    continue
                keys = ('-' + key, '--' + key) if len(key) == 1 else ('--' + key,)
                paa(*keys, action=_FlagAction, flag=value)

        for keys, traitname in aliases.items():
            if not isinstance(keys, tuple):
                keys = (keys,)

            for key in keys:
                argparse_kwds = {
                    'type': str,
                    'dest': traitname.replace(".", _DOT_REPLACEMENT),
                    'metavar': traitname,
                }
                if traitname in argparse_traits:
                    argparse_kwds.update(argparse_traits[traitname][1])
                    if 'action' in argparse_kwds and traitname in alias_flags:
                        # flag sets 'action', so can't have flag & alias with custom action
                        # on the same name
                        raise ArgumentError(
                            "The alias `%s` for the 'append' sequence "
                            "config-trait `%s` cannot be also a flag!'"
                            % (key, traitname))
                if traitname in alias_flags:
                    # alias and flag.
                    # when called with 0 args: flag
                    # when called with >= 1: alias
                    argparse_kwds.setdefault('nargs', '?')
                    argparse_kwds['action'] = _FlagAction
                    argparse_kwds['flag'] = alias_flags[traitname]
                    argparse_kwds['alias'] = traitname
                keys = ('-' + key, '--' + key) if len(key) == 1 else ('--'+ key,)
                paa(*keys, **argparse_kwds)

    def _convert_to_config(self):
        """self.parsed_data->self.config, parse unrecognized extra args via KVLoader."""
        extra_args = self.extra_args

        for lhs, rhs in vars(self.parsed_data).items():
            if lhs == "extra_args":
                self.extra_args = ["-" if a == _DASH_REPLACEMENT else a for a in rhs] + extra_args
                continue
            elif lhs == '_flags':
                # _flags will be handled later
                continue

            lhs = lhs.replace(_DOT_REPLACEMENT, ".")
            if '.' not in lhs:
                # probably a mistyped alias, but not technically illegal
                self.log.warning("Unrecognized alias: '%s', it will have no effect.", lhs)
                trait = None

            if isinstance(rhs, list):
                rhs = DeferredConfigList(rhs)
            elif isinstance(rhs, str):
                rhs = DeferredConfigString(rhs)

            trait = self.argparse_traits.get(lhs)
            if trait:
                trait = trait[0]

            # eval the KV assignment
            try:
                self._exec_config_str(lhs, rhs, trait)
            except Exception as e:
                # cast deferred to nicer repr for the error
                # DeferredList->list, etc
                if isinstance(rhs, DeferredConfig):
                    rhs = rhs._super_repr()
                raise ArgumentError(f"Error loading argument {lhs}={rhs}, {e}")

        for subc in self.parsed_data._flags:
            self._load_flag(subc)


class KeyValueConfigLoader(KVArgParseConfigLoader):
    """Deprecated in traitlets 5.0

    Use KVArgParseConfigLoader
    """
    def __init__(self, *args, **kwargs):
        warnings.warn(
            "KeyValueConfigLoader is deprecated since Traitlets 5.0."
            " Use KVArgParseConfigLoader instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)


def load_pyconfig_files(config_files, path):
    """Load multiple Python config files, merging each of them in turn.

    Parameters
    ----------
    config_files : list of str
        List of config files names to load and merge into the config.
    path : unicode
        The full path to the location of the config files.
    """
    config = Config()
    for cf in config_files:
        loader = PyFileConfigLoader(cf, path=path)
        try:
            next_config = loader.load_config()
        except ConfigFileNotFound:
            pass
        except:
            raise
        else:
            config.merge(next_config)
    return config
